# BuildStream comment cleaner

Remove matching comments from Python, C, and C++ sources in a BuildStream
target's workspaces. BuildStream resolves build and runtime dependencies to
every depth. The cleaner processes shared dependencies once and skips the root
targets unless you pass `--include-targets`.

## Setup

Use Python 3.9 or later and a Python version that supports the syntax used by
the source files. Install BuildStream 1.x or 2.x for your project and make
`bst` available on your PATH. Use `--bst` to select a different executable.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp badwords.example.txt badwords.txt
```

Edit `badwords.txt` as UTF-8 with one literal word or phrase per line. Blank
lines are ignored. Matching ignores case by default. The `FIXME` entry
matches `FIXME`, `fixme`, and `FixMe`. Pass `--ignore-case` to select that
behavior explicitly or `--case-sensitive` to require exact capitalization.
Use `--substring` to match inside larger words, or add individual entries
with repeatable `--word` options.

## Run

Run these commands from the directory containing the cleaner. Replace the
project path and target name with your own.

```bash
# Preview the matching comments.
python bst_comment_cleaner.py --project /path/to/project \
  --target app.bst --words badwords.txt

# Apply edits and pause before each workspace.
python bst_comment_cleaner.py --project /path/to/project \
  --target app.bst --words badwords.txt --apply --step
```

Repeat `--target` to select multiple roots. Both preview and apply reuse
registered workspaces and can open missing workspaces. New workspaces default
to `.bst-comment-workspaces` under the BuildStream project. Specify
`--workspace-dir` to change that location. Workspaces remain open for review.

### Select specific dependencies

Use `--select` to list workspace candidates before opening any workspace.

```bash
python bst_comment_cleaner.py --project /path/to/project \
  --target app.bst --words badwords.txt --select
```

Enter the numbers of the dependencies you want to check, separated by spaces
or commas. Type `all` to select every candidate, or press Enter to skip them
all. Add `--apply` to remove matches in the selected workspaces.

For scripted use, repeat `--dependency` to clean only the named elements.
Use the exact BuildStream element names, including paths under the element
directory or junction prefixes where applicable. Each name must belong to the target's
dependency graph. Dependencies of the selected elements are cleaned only
when named explicitly. With `--include-targets`, you can also name a root
element in the selection.

```bash
python bst_comment_cleaner.py --project /path/to/project \
  --target app.bst --dependency libs/crypto.bst --dependency libs/zlib.bst \
  --words badwords.txt --apply --step
```

Omit both `--select` and `--dependency` to process the entire dependency graph.

For local descriptors, the cleaner uses a non-empty top-level `sources:`
list to identify elements that can open new workspaces. For dependencies in
junctions, BuildStream determines whether a workspace can be opened.

The cleaner blanks matching comments while preserving source line numbers.
It compares Python syntax trees or C/C++ preprocessing tokens before writing.
Matching Python shebangs and encoding declarations are kept and reported
because they can affect execution. The cleaner reports failures and exits
with status 1 if any issues remain. Use `--help` for the available options.
