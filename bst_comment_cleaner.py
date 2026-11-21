#!/usr/bin/env python3
"""Remove matching Python, C, and C++ comments from BuildStream dependencies.

Requires Python 3.9+, PyYAML, and the project's BuildStream CLI. Use a Python
interpreter new enough to parse the Python sources being cleaned. Dry runs
can open and register workspaces. Use --apply to write source edits. The
tool reports matching Python shebangs and encoding declarations as issues
and preserves them because they can affect execution.
"""

import argparse
import ast
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from io import BytesIO, StringIO
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import tokenize

import yaml


IGNORED_DIRS = {".git", ".hg", ".svn", ".bst", ".bst2", "__pycache__"}
PYTHON_SUFFIXES = {".py", ".pyw", ".pyi"}
C_SUFFIXES = {
    ".c", ".h", ".cc", ".cpp", ".cxx", ".c++", ".cp", ".hh", ".hpp",
    ".hxx", ".h++", ".ipp", ".inl", ".tpp", ".ixx", ".cppm", ".ccm",
    ".cxxm", ".c++m",
}
NEWLINE = re.compile(r"\r\n|[\r\n]")
SPLICE = re.compile(r"\\(?:\r\n|[\r\n])")
NONSTANDARD_SPLICE = re.compile(r"\\[ \t\v\f]+[\r\n]")
TRIGRAPH = re.compile(r"\?\?[=/'()!<>-]")
CODING_COOKIE = re.compile(r"^#.*?coding[:=][ \t]*[-\w.]+", re.ASCII)
PYTHON_SHEBANG = re.compile(rb"^#![^\r\n]*\b(?:python|pypy)[0-9.]*\b")
DIRECTIVE_HEADERS = {"include", "include_next", "import", "embed"}
HEADER_PROBES = {"__has_include", "__has_include_next", "__has_embed"}

C_TOKEN = re.compile(
    r"(?P<newline>\r\n|[\r\n])"
    r"|(?P<space>[ \t\v\f]+)"
    r"|(?P<line_comment>//[^\r\n]*)"
    r"|(?P<block_comment>/\*[\s\S]*?\*/)"
    r"|(?P<broken_comment>/\*)"
    r'|(?P<raw>(?:u8|[uUL])?R"(?=[^ ()\\\t\v\f\r\n]{0,16}\())'
    r"""|(?P<quoted>"(?:\\[^\r\n]|[^"\\\r\n])*"|'(?:\\[^\r\n]|[^'\\\r\n])*')"""
    r"""|(?P<number>(?:[0-9]|\.[0-9])(?:[eEpP][+-]|[.\w\x80-\xff]|'[\w\x80-\xff])*)"""
    r"|(?P<identifier>[A-Za-z_$\x80-\xff][\w$\x80-\xff]*)"
    r"|(?P<punct>%:%:|##|%:|[^\r\n])",
    re.ASCII,
)
RAW_DELIMITER = re.compile(r"[^ ()\\\t\v\f\r\n]{0,16}\(")


@dataclass(frozen=True)
class Comment:
    start: int
    end: int
    line: int
    body: str
    protected: str = ""


@dataclass(frozen=True)
class Edit:
    path: Path
    before: bytes
    after: bytes
    comments: int


def blank(text, continuation=False):
    """Keep physical line endings and maintain a separator between tokens."""
    parts = []
    start = 0
    for newline in NEWLINE.finditer(text):
        width = newline.start() - start
        if continuation:
            parts.append(" " * max(width - 1, 0) + "\\")
        else:
            parts.append(" " * width)
        parts.append(newline.group())
        start = newline.end()
    parts.append(" " * (len(text) - start))
    return "".join(parts)


def replace_comments(text, comments, continuation=False):
    parts = []
    cursor = 0
    for comment in comments:
        parts.append(text[cursor:comment.start])
        parts.append(blank(text[comment.start:comment.end], continuation))
        cursor = comment.end
    parts.append(text[cursor:])
    return "".join(parts)


def python_comments(text):
    offsets = [0] + [newline.end() for newline in NEWLINE.finditer(text)]
    normalized = NEWLINE.sub("\n", text)
    for token in tokenize.generate_tokens(StringIO(normalized).readline):
        if token.type != tokenize.COMMENT:
            continue
        line, column = token.start
        start = offsets[line - 1] + column
        protected = ""
        if line == 1 and column == 0 and token.string.startswith("#!"):
            protected = "Python shebang"
        elif line <= 2 and CODING_COOKIE.match(token.string):
            prefix = text[offsets[line - 1]:start]
            first_line = normalized.partition("\n")[0].lstrip(" \t\f")
            if not prefix.strip(" \t\f") and (
                line == 1 or not first_line or first_line.startswith("#")
            ):
                protected = "Python encoding declaration"
        yield Comment(start, start + len(token.string), line, token.string[1:], protected)


class LogicalSource:
    """Map C preprocessing characters back to their physical source offsets."""

    def __init__(self, text):
        if TRIGRAPH.search(text):
            raise ValueError("trigraph interpretation depends on the compiler mode")
        if NONSTANDARD_SPLICE.search(text):
            raise ValueError("whitespace before an escaped newline depends on the compiler mode")
        parts = []
        self.logical_splices = []
        self.removed = [0]
        self.physical_ends = []
        cursor = 0
        for splice in SPLICE.finditer(text):
            parts.append(text[cursor:splice.start()])
            self.logical_splices.append(splice.start() - self.removed[-1])
            self.removed.append(self.removed[-1] + len(splice.group()))
            self.physical_ends.append(splice.end())
            cursor = splice.end()
        parts.append(text[cursor:])
        self.text = "".join(parts)
        self.physical = text
        self.lines = [newline.end() for newline in NEWLINE.finditer(text)]

    def physical_at(self, index):
        return index + self.removed[bisect_right(self.logical_splices, index)]

    def logical_at(self, index):
        return index - self.removed[bisect_right(self.physical_ends, index)]


def header_end(text, start):
    closing = ">" if text[start] == "<" else '"'
    end = text.find(closing, start + 1)
    if end < 0 or NEWLINE.search(text, start + 1, end):
        raise ValueError("unterminated header name")
    return end + 1


def raw_string_end(source, quote):
    physical_quote = source.physical_at(quote)
    delimiter = RAW_DELIMITER.match(source.physical, physical_quote + 1)
    if delimiter is None:
        raise ValueError("line splice in a raw string delimiter")
    closing = ")" + delimiter.group()[:-1] + '"'
    end = source.physical.find(closing, delimiter.end())
    if end < 0:
        raise ValueError("unterminated C++ raw string")
    end += len(closing)
    return source.logical_at(end), source.physical[source.physical_at(quote):end]


def c_tokens(text):
    """Yield comment spans or preprocessing tokens with their separator state."""
    source = LogicalSource(text)
    logical = source.text
    cursor = 3 if logical.startswith("\xef\xbb\xbf") else 0
    previous = ""
    at_line_start = True
    directive = False
    header = False
    probe = False
    separated = False
    while cursor < len(logical):
        match = C_TOKEN.match(logical, cursor)
        kind, spelling = match.lastgroup, match.group()
        end = match.end()

        if header and logical[cursor] in '<"':
            end = header_end(logical, cursor)
            kind, spelling = "header", logical[cursor:end]

        if kind == "broken_comment":
            raise ValueError("unterminated C block comment")
        if kind in {"line_comment", "block_comment"}:
            start = source.physical_at(cursor)
            stop = source.physical_at(end - 1) + 1
            body = spelling[2:] if kind == "line_comment" else spelling[2:-2]
            try:
                body = body.encode("latin-1").decode("utf-8")
            except UnicodeDecodeError:
                pass
            yield Comment(start, stop, bisect_right(source.lines, start) + 1, body)
            separated = True
        elif kind == "space":
            separated = True
        elif kind == "newline":
            yield ("newline", spelling, False)
            at_line_start, directive, header, probe, separated = True, False, False, False, False
        else:
            if kind == "punct" and spelling in {'"', "'"}:
                raise ValueError("unterminated C/C++ string or character literal")
            if kind == "raw":
                end, raw = raw_string_end(source, end - 1)
                spelling = spelling[:-1] + raw
            yield (kind, spelling, separated)
            header = (
                directive and spelling in DIRECTIVE_HEADERS
                or probe and spelling == "("
                or spelling == "import" and (at_line_start or previous in {"export", ";", "}"})
            )
            probe = spelling in HEADER_PROBES
            directive = at_line_start and spelling in {"#", "%:"}
            at_line_start = separated = False
            previous = spelling
        cursor = end


def rewrite(path, data, matches, workspace):
    is_python = path.suffix.lower() in PYTHON_SUFFIXES or bool(PYTHON_SHEBANG.match(data))
    if is_python:
        encoding, _ = tokenize.detect_encoding(BytesIO(data).readline)
        text = data.decode(encoding)
        if text.encode(encoding) != data:
            raise ValueError("source encoding cannot be round-tripped")
        original_ast = ast.parse(data, filename=str(path))
        comments = python_comments(text)
    else:
        encoding = "latin-1"  # One character per byte for C/C++ offsets.
        text = data.decode(encoding)
        tokens = list(c_tokens(text))
        comments = (token for token in tokens if isinstance(token, Comment))

    selected = []
    protected = 0
    for comment in comments:
        if not matches(comment.body):
            continue
        if comment.protected:
            print(f"  Kept {path.relative_to(workspace)}:{comment.line} ({comment.protected})")
            protected += 1
        else:
            selected.append(comment)
    if not selected:
        return None, protected

    result = replace_comments(text, selected, continuation=not is_python)
    encoded = result.encode(encoding)
    if is_python:
        new_ast = ast.parse(encoded, filename=str(path))
        if ast.dump(new_ast, include_attributes=True) != ast.dump(
            original_ast, include_attributes=True
        ):
            raise ValueError("Python syntax tree changed")
    else:
        before = [token for token in tokens if not isinstance(token, Comment)]
        after = [token for token in c_tokens(result) if not isinstance(token, Comment)]
        if before != after:
            raise ValueError("C/C++ preprocessing tokens or separators changed")
    return Edit(path, data, encoded, len(selected)), protected


def walk(root, excluded):
    def on_error(error):
        raise error

    for directory, subdirs, filenames in os.walk(root, onerror=on_error):
        base = Path(directory)
        subdirs[:] = sorted(
            name for name in subdirs
            if name not in IGNORED_DIRS
            and not (base / name).is_symlink()
            and base / name not in excluded
        )
        for name in sorted(filenames):
            path = base / name
            if stat.S_ISREG(path.lstat().st_mode):
                yield path


def source_data(path):
    if path.suffix.lower() in PYTHON_SUFFIXES | C_SUFFIXES:
        return path.read_bytes()
    if not path.suffix:
        with path.open("rb") as stream:
            if PYTHON_SHEBANG.match(stream.readline(4096)):
                stream.seek(0)
                return stream.read()
    return None


def element_root(project):
    config = yaml.safe_load((project / "project.conf").read_bytes())
    if not isinstance(config, dict):
        raise ValueError("project.conf must contain a YAML mapping")
    root = (project / config.get("element-path", ".")).resolve(strict=True)
    if not root.is_dir() or not root.is_relative_to(project):
        raise ValueError("element-path must be a directory inside the project")
    return root


def has_declared_sources(root, element):
    path = (root / element).resolve(strict=True)
    if not path.is_relative_to(root):
        raise ValueError("element descriptor is outside element-path")
    # Inspect the literal top-level keys before YAML merge or BuildStream includes.
    node = yaml.compose(path.read_bytes(), Loader=yaml.SafeLoader)
    if not isinstance(node, yaml.MappingNode):
        raise ValueError("element descriptor must be a YAML mapping")
    sources = [value for key, value in node.value if key.value == "sources"]
    if not sources:
        return False
    if len(sources) != 1 or not isinstance(sources[0], yaml.SequenceNode):
        raise ValueError("expected a single top-level sources sequence")
    return bool(sources[0].value)


class BuildStream:
    def __init__(self, executable, project):
        self.executable = executable
        self.project = project
        version = subprocess.check_output([executable, "--version"], cwd=project, text=True)
        match = re.search(r"(?<![\d.])([12])\.\d+", version)
        if not match:
            raise ValueError(f"expected BuildStream 1.x or 2.x, got {version.strip()!r}")
        self.major = int(match[1])

    def show(self, targets, deps):
        output = subprocess.check_output(
            [self.executable, "--no-colors", "show", "--deps", deps,
             "--format", "%{name}", "--", *targets],
            cwd=self.project, text=True,
        )
        names = [line.strip() for line in output.splitlines() if line.strip()]
        for name in names:
            for component in name.split(":"):
                path = Path(component)
                if path.suffix != ".bst" or path.is_absolute() or ".." in path.parts:
                    raise ValueError(f"unexpected element name from bst show: {name!r}")
        return names

    def dependency_pipeline(self, targets, include_targets):
        pipeline = self.show(targets, "all")
        if not pipeline:
            raise ValueError("BuildStream returned no elements for the supplied targets")
        roots = set() if include_targets else set(self.show(targets, "none"))
        if not include_targets and (not roots or not roots.issubset(pipeline)):
            raise ValueError("BuildStream did not identify the roots in the dependency graph")
        return [element for element in dict.fromkeys(pipeline) if element not in roots]

    def workspaces(self):
        output = subprocess.check_output(
            [self.executable, "workspace", "list"], cwd=self.project, text=True
        )
        data = yaml.safe_load(output)
        if not isinstance(data, dict) or not isinstance(data.get("workspaces"), list):
            raise ValueError("unexpected output from bst workspace list")
        return {
            item["element"]: (self.project / item["directory"]).resolve()
            for item in data["workspaces"]
        }

    def open(self, element, directory):
        directory.parent.mkdir(parents=True, exist_ok=True)
        name = ["--", element]
        if self.major == 1:
            args = [*name, str(directory)]
        else:
            args = ["--directory", str(directory), *name]
        command = [self.executable, "--no-colors", "workspace", "open", *args]
        if ":" in element:
            # BuildStream loads external junction descriptors itself.
            result = subprocess.run(command, cwd=self.project, text=True, stderr=subprocess.PIPE)
            if result.returncode and re.search(
                r"The (?:given element|element [^\r\n]*?) +has no sources\b", result.stderr
            ):
                return None
            if result.stderr:
                print(result.stderr, end="", file=sys.stderr)
            result.check_returncode()
        else:
            subprocess.run(command, cwd=self.project, check=True)
        workspace = self.workspaces().get(element)
        if workspace is None:
            raise ValueError("BuildStream did not register the element under its descriptor name")
        return workspace


def save(edit):
    path = edit.path
    mode = path.lstat()
    if not stat.S_ISREG(mode.st_mode) or path.read_bytes() != edit.before:
        raise ValueError("file changed after scanning")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".bst-comment-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(edit.after)
            os.fchmod(stream.fileno(), stat.S_IMODE(mode.st_mode))
            stream.flush()
            os.fsync(stream.fileno())
        current = path.lstat()
        if (current.st_ino, current.st_dev, current.st_mtime_ns, current.st_size) != (
            mode.st_ino, mode.st_dev, mode.st_mtime_ns, mode.st_size
        ):
            raise ValueError("file changed after scanning")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def clean_workspace(workspace, excluded, matches, apply):
    if not workspace.is_dir():
        raise ValueError(f"workspace directory is missing: {workspace}")
    files = removed = errors = 0
    for path in walk(workspace, excluded):
        try:
            data = source_data(path)
            if data is None:
                continue
            edit, protected = rewrite(path, data, matches, workspace)
            errors += protected
            if edit is None:
                continue
            if apply:
                save(edit)
            files += 1
            removed += edit.comments
            print(f"  {path.relative_to(workspace)}: {edit.comments} matching comment(s)")
        except (OSError, ValueError, SyntaxError, tokenize.TokenError, RecursionError) as error:
            print(f"  ERROR {path.relative_to(workspace)}: {error}", file=sys.stderr)
            errors += 1
    return files, removed, errors


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path.cwd(), help="directory with project.conf")
    parser.add_argument("--bst", default="bst", help="BuildStream executable")
    parser.add_argument("--target", required=True, action="append", metavar="ELEMENT.bst",
                        help="root element whose transitive dependencies to clean; repeatable")
    parser.add_argument("--include-targets", action="store_true",
                        help="clean the root elements as well as their dependencies")
    parser.add_argument("--workspace-dir", type=Path, default=Path(".bst-comment-workspaces"),
                        help="directory for new workspaces, relative to the project by default")
    parser.add_argument("--words", type=Path, help="UTF-8 file with one word or phrase per line")
    parser.add_argument("--word", action="append", default=[], help="additional literal; repeatable")
    parser.add_argument("--substring", action="store_true", help="match inside larger words")
    case = parser.add_mutually_exclusive_group()
    case.add_argument("--ignore-case", dest="case_sensitive", action="store_false",
                      help="ignore capitalization when matching words (default)")
    case.add_argument("--case-sensitive", action="store_true",
                      help="require exact capitalization when matching words")
    parser.set_defaults(case_sensitive=False)
    parser.add_argument("--apply", action="store_true", help="write edits; otherwise report matches")
    parser.add_argument("--step", action="store_true", help="pause before each workspace")
    args = parser.parse_args()
    if args.step and not sys.stdin.isatty():
        parser.error("--step requires an interactive terminal")
    return args


def main():
    args = arguments()
    failures = (OSError, ValueError, TypeError, KeyError, yaml.YAMLError, subprocess.CalledProcessError)
    try:
        words = args.word + (args.words.read_text(encoding="utf-8-sig").splitlines() if args.words else [])
        literals = sorted({word.strip() for word in words if word.strip()}, key=lambda word: (-len(word), word))
        if not literals:
            raise ValueError("provide at least one nonblank entry via --words or --word")
        pattern = "|".join(re.escape(word) for word in literals)
        if not args.substring:
            pattern = rf"(?<!\w)(?:{pattern})(?!\w)"
        matches = re.compile(pattern, 0 if args.case_sensitive else re.IGNORECASE).search

        project = args.project.resolve(strict=True)
        if not (project / "project.conf").is_file():
            raise ValueError(f"no project.conf in {project}")
        destination = (project / args.workspace_dir).resolve()
        if destination == project or project.is_relative_to(destination):
            raise ValueError("--workspace-dir cannot contain the project directory")

        root = element_root(project)
        bst = BuildStream(args.bst, project)
        pipeline = bst.dependency_pipeline(args.target, args.include_targets)
        registered = bst.workspaces()
        excluded = {destination, *registered.values()}
    except failures as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1

    errors = 0
    elements = []
    for element in pipeline:
        try:
            if element in registered or ":" in element or has_declared_sources(root, element):
                elements.append(element)
        except (OSError, ValueError, yaml.YAMLError) as error:
            print(f"ERROR {element}: {error}", file=sys.stderr)
            errors += 1

    print(f"{len(elements)} workspace candidate(s) from {len(pipeline)} selected element(s).", flush=True)
    total_files = total_comments = 0
    for number, element in enumerate(elements, 1):
        print(f"[{number}/{len(elements)}] {element}", flush=True)
        if args.step:
            if input("Press Enter to proceed, or type q to stop: ").strip().casefold() == "q":
                return 130
        try:
            workspace = registered.get(element)
            if workspace is None:
                candidate = (destination / (element + ".workspace")).resolve()
                if not candidate.is_relative_to(destination) or candidate.exists():
                    raise ValueError(f"workspace destination is occupied or outside its root: {candidate}")
                workspace = bst.open(element, candidate)
                if workspace is None:
                    print("  Skipped: BuildStream reports that this element has no sources.")
                    continue
                excluded.add(workspace)
            print(f"  Workspace: {workspace}", flush=True)
            files, comments, issues = clean_workspace(workspace, excluded, matches, args.apply)
            total_files += files
            total_comments += comments
            errors += issues
        except failures as error:
            print(f"  ERROR: {error}", file=sys.stderr)
            errors += 1
    action = "Removed" if args.apply else "Would remove"
    print(f"{action} {total_comments} comment(s) in {total_files} file(s). {errors} issue(s).")
    return 1 if errors else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (KeyboardInterrupt, EOFError):
        print("\nStopped.", file=sys.stderr)
        sys.exit(130)
