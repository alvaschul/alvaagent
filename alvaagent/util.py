import datetime
import os
import re
import urllib.request

try:
    import yaml
except Exception:
    yaml = None

_SKILL_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)

_SKILL_FM_DEFAULT = {
    "name": None,       # filled from the filename on save
    "description": None,
    "version": None,
    "author": None,
    "tags": [],
    "related_skills": [],
}

_VALID_FM_KEYS = frozenset(("name", "description", "version", "author", "tags", "related_skills"))


def _mini_scalar(v):
    """Coerce a bare frontmatter scalar to a Python value (strips quotes,
    parses inline '[a, b]' arrays so tags: [x, y] works without PyYAML)."""
    s = v.strip()
    if s.startswith("[") and s.endswith("]"):
        inner = s[1:-1].strip()
        if not inner:
            return []
        return [_mini_scalar(x.strip()) for x in inner.split(",")]
    if s.lower() == "true":
        return True
    if s.lower() == "false":
        return False
    if s.lower() in ("null", "none", "~"):
        return None
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        return s[1:-1]
    return s


def _finish_block(kind, lines, chomp):
    """Render collected block-scalar lines: '>' folds to a single space, '|'
    preserves newlines. Chomping: '-' strips the trailing newline, '+' keeps
    it, default (clip) yields a single trailing newline."""
    if kind == ">":
        text = " ".join(x for x in lines if x)
    else:
        text = "\n".join(lines)
    text = text.rstrip()
    if chomp == "+":
        text += "\n"
    return text


def _mini_yaml(text):
    """Tiny YAML-subset parser for the frontmatter format the harness writes:
    'key: scalar' lines, 'key:' followed by '  - item' list entries, and
    block scalars ('key: >' / 'key: |' with indented continuation lines).
    Used only when PyYAML isn't installed."""
    out = {}
    current_list = None
    block_kind = None
    block_key = None
    block_chomp = ""
    block_lines = []
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if (line.startswith(" ") or line.startswith("\t")) and block_kind is not None:
            block_lines.append(line.strip())
            continue
        if block_kind is not None:
            out[block_key] = _finish_block(block_kind, block_lines, block_chomp)
            block_kind = None
            block_lines = []
        if line.strip().startswith("- "):
            item = line.split("- ", 1)[1].strip()
            if current_list is not None:
                current_list.append(_mini_scalar(item))
            continue
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        if not key or key.startswith(" "):
            continue
        val = val.strip()
        if val in (">", "|", ">-", "|-", ">+", "|+"):
            block_kind = val[0]
            block_chomp = val[1:]
            block_key = key
            block_lines = []
            continue
        if val == "":
            out[key] = []
            current_list = out[key]
        else:
            out[key] = _mini_scalar(val)
            current_list = None
    if block_kind is not None:
        out[block_key] = _finish_block(block_kind, block_lines, block_chomp)
    return out


def _frontmatter_load(raw):
    """Parse skill frontmatter with PyYAML when available, else the mini parser."""
    if yaml is not None:
        try:
            loaded = yaml.safe_load(raw)
            if isinstance(loaded, dict):
                return loaded
        except Exception:
            pass
    try:
        loaded = _mini_yaml(raw)
    except Exception:
        loaded = {}
    return loaded if isinstance(loaded, dict) else {}


def _frontmatter_dump(fm):
    """Serialize frontmatter to the YAML block (PyYAML when available, else the
    mini writer, so skill_save works with zero extra installs)."""
    if yaml is not None:
        try:
            return yaml.safe_dump(fm, sort_keys=False, allow_unicode=True).rstrip()
        except Exception:
            pass
    lines = []
    for k, v in fm.items():
        if v is None:
            continue
        if isinstance(v, list):
            if not v:
                lines.append("%s: []" % k)
            else:
                lines.append("%s:" % k)
                for item in v:
                    lines.append("  - %s" % str(item))
        elif isinstance(v, bool):
            lines.append("%s: %s" % (k, "true" if v else "false"))
        else:
            lines.append("%s: %s" % (k, v))
    return "\n".join(lines)


def _parse_frontmatter(text):
    """Return (fm_dict, body) from a skill .md file. fm_dict shares the default
    template so callers always get the same keys; unknown frontmatter keys are
    dropped (like Hermes' strict frontmatter model)."""
    fm = dict(_SKILL_FM_DEFAULT)
    body = text
    m = _SKILL_FM_RE.match(text)
    if m:
        raw = m.group(1)
        body = text[m.end():]
        loaded = _frontmatter_load(raw)
        if isinstance(loaded, dict):
            for k in _VALID_FM_KEYS:
                v = loaded.get(k)
                if v is not None:
                    if k in ("tags", "related_skills") and isinstance(v, list):
                        fm[k] = [str(x) for x in v]
                    elif k == "description":
                        fm[k] = str(v)
                    elif k in ("version", "author"):
                        fm[k] = str(v) if v is not None else None
                    else:
                        fm[k] = v
    return fm, body


_SKILL_RAW_MAX = 300_000  # cap for a fetched skill body (chars)


def _looks_like_html(raw):
    """True when a fetched body looks like an HTML page rather than markdown."""
    head = str(raw).lstrip()[:120].lower()
    return head.startswith("<html") or head.startswith("<!doctype")


def _raw_fetch(url):
    """Fetch a URL's raw body (up to _SKILL_RAW_MAX). Returns None on network
    failure or when the response looks like an HTML page rather than markdown
    (e.g. a GitHub repo page that isn't a raw file)."""
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "alvaagent-tui/1.0", "Accept-Encoding": "identity"})
        with urllib.request.urlopen(req, timeout=30) as r:
            if r.getcode() >= 400:
                return None
            raw = r.read(_SKILL_RAW_MAX).decode("utf-8", errors="replace")
    except Exception:
        return None
    if _looks_like_html(raw):
        return None
    return raw


def _atomic_write(path, text, mode="w"):
    """Write text to `path` atomically: temp file + fsync + rename into place.
    Creates parent dirs. Raises on failure so callers can report the error."""
    import tempfile
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)) or ".",
                               prefix=".tmp.", suffix=".write")
    try:
        with os.fdopen(fd, mode, encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _env(*names):
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return None


def _fmt_k(n):
    n = int(n)
    return "%.1fk" % (n / 1000.0) if n >= 1000 else str(n)


def now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")


def mask_key(key):
    return "****" if key else "(none)"
