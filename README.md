# UT Analyzer

A desktop application that analyzes a large existing software repository and improves its unit tests.

UT Analyzer scans a repository, detects its language / build system / test framework, analyzes the
quality and conventions of existing unit tests, generates new convention-matching tests using your
**company's internal LLM API**, validates them (syntax, compile, run, duplicate detection), and
applies only the changes the user approves.

## Features

- Repository scanning with a checkbox folder tree (choose exactly what gets analyzed).
- Automatic environment detection: language, test framework, build system, build & test commands,
  mocking libraries. Every field is editable when detection is not possible.
- Existing test convention analysis (naming, fixtures, mocks, assertion style, imports, helpers)
  so generated tests look like they were written as part of the project.
- Test quality analysis and a status report (weak files, untested functions, trivial tests).
- Test generation via corporate OpenAI-compatible API (base URL, API key, and model in Settings).
- Review workflow: one tab per proposed file with a diff, per-test approve / reject / edit.
- Validation pipeline: syntax check -> compile -> run -> duplicate detection, per test.
- Before / after report with measured test counts and validation results.

## Supported languages

| Tier | Languages | Capabilities |
|------|-----------|--------------|
| 1    | C++, Python | detection, conventions, generation, compile+run validation |
| 2    | JavaScript/TypeScript, Java, C#, Go | detection, generation, validation where tooling exists |

## Requirements (development)

- Python 3.11+
- Access to your company's internal LLM API (OpenAI-compatible chat completions)
- API credentials configured in **Settings** (base URL, API key, model name)
- The analyzed project's own toolchain (compiler / package manager / test runner) must be
  installed for compile/run validation. Without it, tests are still generated but
  marked "not validated" with an explanation.

## Run (development)

```bash
pip install -r requirements.txt
python -m ut_analyzer.main
```

On first launch, open **Settings** and enter your corporate API base URL, API key, and model name.

## Production Windows installer

```powershell
.\scripts\build_release.ps1
```

Output: `dist\installer\UTAnalyzer-Setup-1.0.0.exe`

See [docs/PRODUCTION_DISTRIBUTION.md](docs/PRODUCTION_DISTRIBUTION.md) for architecture and
verification checklist.

## Architecture

- `ut_analyzer/ui` — PySide6 widgets (main window, folder tree, proposal tabs, diff viewer...).
- `ut_analyzer/core` — scanning, detection, discovery, conventions, quality, generation,
  validation, diffing/approval, reporting, orchestration.
- `ut_analyzer/adapters` — one language adapter per supported language behind a common interface.
- `ut_analyzer/models` — plain dataclasses shared by all layers.
- `tests/` — the application's own unit tests.
