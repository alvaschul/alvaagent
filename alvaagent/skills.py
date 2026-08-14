import os
import re
import subprocess

from alvaagent.permissions import request_permission
from alvaagent.util import (
    _atomic_write, _raw_fetch, _parse_frontmatter, _frontmatter_dump,
)

# ---------------- skills: Hermes-style frontmatter + categorized storage ----------------
# Skills mirror Hermes: each skill file is a Markdown doc with an optional YAML
# frontmatter block between `---` fences. The frontmatter carries `name`,
# `description`, optional `version`/`author`/`tags`/`related_skills`, and the
# body is the procedure itself (trigger + numbered steps).
#
# Storage layout (Hermes-style):
#   ~/.alvaagent/skills/<category>/<name>.md
#     e.g. skills/productivity/product-price-monitor.md
#     e.g. skills/research/competitor-news-monitor.md
# `<category>` is a slash-free folder name; `<name>.md` is the skill filename.
# `skill_save` writes into SKILLS_DIR/<category>/<name>.md when a category
# is supplied, otherwise falls back to the legacy flat layout so old skills keep
# working. `skill_list` returns [{"name": ..., "category": ..., "file": ...,
# "description": ..., "tags": ..., "related_skills": ...}, ...] and strips the
# legacy flat names so callers that only want names still work.
#
# Backward compat: flat files (skills/<name>.md with no category folder) are
# still readable and listable. On save, if `category` is omitted or empty the
# skill lives flat; if supplied it goes into the categorized layout. `skill_read`
# accepts either "name" (flat) or "category/name" (categorized).



def _skill_body_for_tool(fm, body):
    """Render a skill's frontmatter as a one-line description the agent can
    scan, then the full body. This is what skill_read returns as 'content'
    so the agent sees metadata + procedure in one call (Hermes injects the full
    SKILL.md including frontmatter into context)."""
    parts = []
    if fm.get("description"):
        parts.append("# " + fm["description"])
    if fm.get("tags"):
        parts.append("")
        parts.append("tags: " + ", ".join(str(t) for t in fm["tags"]))
    if fm.get("related_skills"):
        parts.append("related: " + ", ".join(str(r) for r in fm["related_skills"]))
    if body.strip():
        if parts:
            parts.append("")
        parts.append(body.rstrip())
    return "\n".join(parts).strip() or body.rstrip()


def _detect_category(name):
    """If `name` already contains a slash, treat the left of it as a category
    and the right as the skill name (so "category/name" works everywhere)."""
    if "/" in name:
        cat, _, nm = name.partition("/")
        return cat.strip(), nm.strip()
    return None, name.strip()


def _skill_filepath(rt, category, name):
    if category:
        return os.path.join(rt.skills_dir, category, name + ".md")
    return os.path.join(rt.skills_dir, name + ".md")


def _inside_skills(rt, path):
    """True when `path` (realpath) lives inside the runtime's skills dir.
    Guards every skill-path operation against `..` traversal
    writing/reading/deleting files elsewhere on the device."""
    real = os.path.realpath(path)
    base = os.path.realpath(rt.skills_dir)
    return real == base or real.startswith(base + os.sep)


def _resolve_skill_path(rt, name):
    """Map a skill name to a real .md file inside the runtime's skills dir.

    Accepts flat ("frontend-design"), category/name ("brainstorming/x"), and
    the frontmatter name of a categorized skill ("brainstorming", which lives
    at skills/brainstorming/SKILL.md). Returns the resolved absolute path, or
    None when nothing matches / the path escapes the skills dir.
    """
    category, skill_name = _detect_category(str(name))
    if skill_name:
        direct = os.path.realpath(_skill_filepath(rt, category, skill_name))
        if os.path.isfile(direct) and _inside_skills(rt, direct):
            return direct
    # Fallback: scan the index and match by frontmatter name (+ category when
    # the caller qualified it). This is how categorized files whose filename
    # differs from their `name:` (e.g. category/SKILL.md) stay reachable.
    want = str(name).strip().lower()
    for info in _skill_list_all(rt):
        nm = str(info.get("name") or "").lower()
        if nm != want:
            continue
        cat = str(info.get("category") or "").lower()
        if category and cat != category.lower():
            continue
        p = os.path.realpath(os.path.join(rt.skills_dir, info["file"]))
        if _inside_skills(rt, p):
            return p
    return None


def _skill_read(path):
    """Parse a skill .md file into its metadata dict plus the body the agent
    applies. Returns None when the file is missing or unreadable. This backs
    _skill_list_all() and skill_read()."""
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except Exception:
        return None
    fm, body = _parse_frontmatter(text)
    # Skills without a `name:` frontmatter key still deserve a usable name:
    # fall back to the filename so the banner and /skills can't crash on None.
    name = fm.get("name") or os.path.splitext(os.path.basename(path))[0]
    return {
        "name": name,
        "description": fm.get("description"),
        "version": fm.get("version"),
        "author": fm.get("author"),
        "tags": fm.get("tags"),
        "related_skills": fm.get("related_skills"),
        "content": _skill_body_for_tool(fm, body),
    }


def _scan_skill_files(rt):
    """Walk the runtime's skills dir and yield (category_or_None, name,
    filepath) for every .md file, including legacy flat files. Categorized
    files take precedence: a file under skills/<cat>/<name>.md is NOT confused
    with a flat skills/<cat>.md (the latter is only produced by old saves)."""
    if not os.path.isdir(rt.skills_dir):
        return
    for entry in os.listdir(rt.skills_dir):
        full = os.path.join(rt.skills_dir, entry)
        if os.path.isfile(full) and entry.endswith(".md"):
            yield None, entry[:-3], full
        elif os.path.isdir(full):
            cat = entry
            for sub in os.listdir(full):
                sub_full = os.path.join(full, sub)
                if os.path.isfile(sub_full) and sub.endswith(".md"):
                    yield cat, sub[:-3], sub_full


def _skill_list_all(rt):
    """Scan the runtime's skills dir and return a list of skill dicts
    (Hermes-style).

    Each dict has: name, category, file, description, tags, related_skills.
    Flat files (no category folder) get category=None; categorized files get
    their folder name. Frontmatter is parsed from each .md file.
    """
    skills = []
    for cat, name, path in _scan_skill_files(rt):
        info = _skill_read(path)
        if info is None:
            continue
        info["category"] = cat
        info["file"] = os.path.relpath(path, rt.skills_dir)
        skills.append(info)
    return skills


def skill_list(rt):
    """List every skill on the device with metadata (Hermes-style).

    Returns {"ok": True, "skills": [dict, ...]} where each dict has:
      name, category, file, description, tags, related_skills.
    When the caller only wants names it can read d["name"].
    """
    try:
        return {"ok": True, "skills": _skill_list_all(rt)}
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}


def skill_read(rt, name):
    """Read a skill by name (flat) or category/name (categorized).

    Returns {"ok": True, "name": ..., "category": ..., "file": ...,
             "description": ..., "tags": ..., "content": ...}
    where 'content' is the frontmatter-annotated body the agent applies.
    """
    name = str(name).strip()
    if not name:
        return {"ok": False, "error": "empty name"}
    path = _resolve_skill_path(rt, name)
    if path is None:
        return {"ok": False, "error": "no such skill: %s" % name}
    info = _skill_read(path)
    if info is None:
        return {"ok": False, "error": "no such skill: %s" % name}
    rel = os.path.relpath(path, rt.skills_dir)
    info["category"] = os.path.dirname(rel) if "/" in rel else None
    info["file"] = rel
    return {"ok": True, **info}


def skill_remove(rt, name):
    """Delete a skill by name (flat) or category/name (categorized).

    Returns {"ok": True} on success, {"ok": False, "error": ...} otherwise.
    """
    name = str(name).strip()
    if not name:
        return {"ok": False, "error": "empty name"}
    path = _resolve_skill_path(rt, name)
    if path is None or not _inside_skills(rt, path):
        return {"ok": False, "error": "no such skill: %s" % name}
    try:
        os.remove(path)
        rel = os.path.relpath(path, rt.skills_dir)
        return {"ok": True, "name": os.path.splitext(os.path.basename(path))[0],
                "category": os.path.dirname(rel) if "/" in rel else None}
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}


def skill_save(rt, name, content, category=None):
    """Save a skill with optional YAML frontmatter.

    `name` may be "skill-name" (flat) or "category/skill-name" (categorized);
    the explicit `category` kwarg overrides any slash in `name`.
    `content` is the full .md source including an optional leading `---` block.
    If no frontmatter is present, one is synthesized from the name so every
    skill carries at least a name (Hermes-style self-describing skills).
    """
    name = str(name).strip()
    if not name or "/" in name:
        return {"ok": False, "error": "invalid skill name: %r (use category kwarg for folders)" % name}
    if category:
        cat = str(category).strip()
        # `..` and absolute paths must not escape SKILLS_DIR
        if "/" in cat or "\\" in cat or not cat or ".." in cat.split(os.sep) or cat.startswith("/"):
            return {"ok": False, "error": "invalid category: %r" % category}
    else:
        cat = None
    if not content:
        content = ""
    fm, body = _parse_frontmatter(content)
    # ensure name is always present
    if not fm.get("name"):
        fm["name"] = name
    if not fm.get("description"):
        fm["description"] = name.replace("-", " ").replace("_", " ").title()
    path = _skill_filepath(rt, cat, name)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # rebuild the .md source with a clean frontmatter block
        fm_block = "---\n" + _frontmatter_dump(
            {k: fm[k] for k in ("name", "description", "version", "author", "tags", "related_skills")
             if fm.get(k) is not None}) + "\n---\n\n"
        full = fm_block + body
        _atomic_write(path, full)
        return {"ok": True, "name": name, "category": cat, "path": path,
                "chars": len(full)}
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}


# ---------------- skills: install from URL / git repo ----------------


def skill_install(rt, source, category=None):
    """Install a skill from a local .md file, a raw .md URL, or a GitHub URL.

    GitHub repo/blob URLs are rewritten to raw.githubusercontent.com so the
    full markdown is fetched (not the web_fetch snippet), parsed for
    frontmatter, and saved into the runtime's skills dir via skill_save.
    Returns the installed skill's name/category/path.
    """
    source = str(source or "").strip()
    if not source:
        return {"ok": False, "error": "empty source"}
    if os.path.exists(source):
        try:
            with open(source, encoding="utf-8", errors="replace") as f:
                content = f.read()
        except Exception as e:
            return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
        name = os.path.basename(source)
        if name.lower().endswith(".md"):
            name = name[:-3]
        return skill_save(rt, name, content, category)
    if source.startswith(("http://", "https://")):
        url = source
        if "github.com/" in url and "/blob/" in url:
            m = re.match(r"https?://github\.com/([^/]+)/([^/]+)/blob/(.+)$", url)
            if m:
                url = "https://raw.githubusercontent.com/%s/%s/%s" % (
                    m.group(1), m.group(2), m.group(3))
        content = _raw_fetch(url)
        if content is None:
            return {"ok": False, "error":
                    "could not fetch skill from %s (network error or non-markdown page)" % url}
        name = os.path.basename(url)
        if name.lower().endswith(".md"):
            name = name[:-3]
        fm, _ = _parse_frontmatter(content)
        if fm.get("name"):
            name = str(fm["name"])
        if not name or name in (".", ""):
            return {"ok": False, "error": "cannot determine a skill name from %s" % url}
        return skill_save(rt, name, content, category)
    return {"ok": False, "error": "source must be a local path or an http(s) URL"}


def skill_sync_repo(rt, repo, subdir=None):
    """Clone a git repo of skills and import every .md as a skill.

    Categories come from each file's folder (top-level folder only - nested
    folders are collapsed to their first component). README.md and .github are
    skipped. Permission-gated (network + disk writes) like run_command.
    """
    repo = str(repo or "").strip()
    if not repo:
        return {"ok": False, "error": "empty repo URL"}
    if not request_permission(rt, "clone skills repo: %s" % repo[:160]):
        return {"ok": False, "error": "permission denied by user"}
    import tempfile, shutil
    tmp = tempfile.mkdtemp(prefix="alva_skills_")
    try:
        try:
            proc = subprocess.run(
                ["git", "clone", "--depth", "1", "--quiet", repo, tmp],
                capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "git clone timed out after 120s"}
        if proc.returncode != 0:
            return {"ok": False, "error": "git clone failed: %s"
                    % (proc.stderr or proc.stdout or "").strip()[:500]}
        root = os.path.join(tmp, str(subdir).strip()) if str(subdir or "").strip() else tmp
        # most skills repos nest everything under a top-level `skills/` folder -
        # treat that as the import root so folder names become real categories.
        if not str(subdir or "").strip() and os.path.isdir(os.path.join(tmp, "skills")):
            root = os.path.join(tmp, "skills")
        if not os.path.isdir(root):
            return {"ok": False, "error": "subdir %r not found in the repo" % subdir}
        if not os.path.realpath(root).startswith(os.path.realpath(tmp)):
            return {"ok": False, "error": "subdir %r escapes the clone directory" % subdir}
        installed, skipped, errors = [], [], []
        for dirpath, _dirs, files in os.walk(root):
            for f in sorted(files):
                if not f.lower().endswith(".md"):
                    continue
                rel = os.path.relpath(dirpath, root)
                relpath = os.path.join("" if rel == "." else rel, f)
                parts = [p.lower() for p in relpath.split(os.sep)]
                if f.lower() == "readme.md" or ".github" in parts:
                    skipped.append(relpath)
                    continue
                try:
                    with open(os.path.join(dirpath, f), encoding="utf-8", errors="replace") as fh:
                        content = fh.read()
                except Exception as e:
                    errors.append((relpath, "%s: %s" % (type(e).__name__, e)))
                    continue
                name = f[:-3] if f.lower().endswith(".md") else f
                fm, _ = _parse_frontmatter(content)
                if fm.get("name"):
                    name = str(fm["name"])
                category = rel.split(os.sep)[0] if rel not in (".", "") else None
                r = skill_save(rt, name, content, category)
                if r.get("ok"):
                    installed.append({"name": r["name"], "category": r.get("category"),
                                      "path": r.get("path")})
                else:
                    errors.append((relpath, r.get("error", "?")))
        return {"ok": True, "count": len(installed), "installed": installed,
                "skipped": skipped, "errors": errors}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
