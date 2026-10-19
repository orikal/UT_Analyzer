"""Builds LLM prompts that embed environment, conventions and testing gaps."""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path

from ...models import (
    GAP_ADDITIONAL_CASES,
    EnvironmentInfo,
    QualityGap,
    TestConventions,
    UTKnowledge,
)
from .llm_settings import (
    DEFAULT_MAX_EXISTING_TEST_CHARS,
    DEFAULT_MAX_SOURCE_CHARS,
    DEFAULT_MAX_UT_LIBRARY_CHARS,
)

TEST_START = "=== TEST:"
TEST_END = "=== END TEST ==="
UT_HEADER_START = "=== UT HEADER ==="
UT_HEADER_END = "=== END UT HEADER ==="
UT_FIXTURE_START = "=== UT FIXTURE ==="
UT_FIXTURE_END = "=== END UT FIXTURE ==="

SYSTEM_PROMPT = (
    "You are an expert software engineer who writes high-quality unit tests. "
    "You write tests that look exactly like they were written by the project's own team: "
    "same naming, same fixtures, same assertion style, same formatting. "
    "You focus on meaningful scenarios and relevant edge cases (boundary values, empty inputs, "
    "error paths, invalid arguments, overflow, null/None handling). "
    "You never test trivial getters or restate the implementation. "
    "You are strict about types: every variable, argument and expected value you write must "
    "match the exact declared type of the API under test - you never initialize or assign a "
    "value whose type differs from the declared one, and you never mix types across a "
    "variable's lifetime. "
    "You never initialize a non-pointer variable or member with null (nullptr/NULL/None) and "
    "you never read from, dereference or call members on a variable that is still null - both "
    "break compilation or crash the test. "
    "You respect language access control, but you never let it stop you: you find the "
    "legitimate way to reach a protected, private, internal or underscore-prefixed member "
    "(a test subclass that re-exports protected members, an existing friend/FRIEND_TEST "
    "name, a `#define private public` include shim inside the test header, reflection, "
    "name mangling, same-package tests) and you test it. You never skip a member, and you "
    "never write a TODO saying that something is inaccessible. "
    "Every test must include English comments placed directly above the test function header, "
    "using the labels explenation, Input Params, and Expected Result. "
    "You answer immediately with the requested test blocks and nothing else: no planning, "
    "no thinking out loud, no restating the task, no analysis before or after the code. "
    "You never repeat yourself. When something is unclear, you write your best guess and mark "
    "it with a TODO comment instead of deliberating about it."
)

_MAX_SOURCE_CHARS = DEFAULT_MAX_SOURCE_CHARS
_MAX_EXISTING_TEST_CHARS = DEFAULT_MAX_EXISTING_TEST_CHARS
_MAX_UT_LIBRARY_CHARS = DEFAULT_MAX_UT_LIBRARY_CHARS

# How many dependency paths the file map lists before it stops.
_MAX_MAPPED_DEPENDENCIES = 25


def build_file_map(
    source_file: Path,
    *,
    root: Path | None = None,
    target_test_file: Path | None = None,
    target_is_new: bool = False,
    header_file: Path | None = None,
    header_is_new: bool = False,
    related_files: Sequence[Path] = (),
    dependency_files: Sequence[Path] = (),
    test_dir: Path | None = None,
    knowledge_files: Sequence[Path] = (),
    max_dependencies: int = _MAX_MAPPED_DEPENDENCIES,
) -> str:
    """Point the model at the real files this generation is about.

    The rest of the prompt only quotes excerpts of these files (truncated to fit
    the context window). Listing their absolute paths lets a file-aware client
    open the originals instead of guessing, and tells every other model which
    files the quoted excerpts came from.
    """
    lines = [
        f"- Source file under test: `{source_file}`",
    ]
    for path in related_files:
        lines.append(f"- Declares its API (compile the tests against it): `{path}`")
    if target_test_file is not None:
        state = " - does not exist yet, it will be created" if target_is_new else ""
        lines.append(f"- Write the tests into: `{target_test_file}`{state}")
    if header_file is not None:
        state = " - does not exist yet, it will be created" if header_is_new else ""
        lines.append(f"- Companion unit-test header: `{header_file}`{state}")
    if test_dir is not None:
        lines.append(f"- The project's existing unit tests live in: `{test_dir}`")
    for path in knowledge_files:
        lines.append(f"- Project knowledge written by the scan: `{path}`")
    seen: set[str] = set()
    dependencies: list[Path] = []
    for path in dependency_files:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        dependencies.append(path)
        if len(dependencies) >= max_dependencies:
            break
    if dependencies:
        lines.append("- Files the code under test depends on:")
        lines.extend(f"  - `{path}`" for path in dependencies)
    if root is not None:
        lines.append(f"- Repository root: `{root}`")
    return (
        "Files for this task - real paths on this machine. Everything quoted further "
        "down in this prompt is an excerpt of these files and may be truncated. Base "
        "every test on what these files actually contain; if you are able to read "
        "files, open them first, starting with the source file under test:\n"
        + "\n".join(lines)
    )


def build_generation_prompt(
    source_code: str,
    source_name: str,
    gap: QualityGap,
    env: EnvironmentInfo,
    conventions: TestConventions,
    existing_test_content: str,
    max_tests: int,
    *,
    target_test_file: str = "",
    test_framework: str | None = None,
    header_file_name: str = "",
    header_template: str = "",
    fixture_template: str = "",
    related_files: list[tuple[str, str]] | None = None,
    file_map: str = "",
    api_context: str = "",
    repo_context: str = "",
    repo_knowledge_file: str = "",
    knowledge_docs: str = "",
    construction_notes: str = "",
    access_notes: str = "",
    ut_knowledge: UTKnowledge | None = None,
    max_source_chars: int = _MAX_SOURCE_CHARS,
    max_existing_test_chars: int = _MAX_EXISTING_TEST_CHARS,
    max_ut_library_chars: int = _MAX_UT_LIBRARY_CHARS,
    max_total_chars: int | None = None,
) -> str:
    """Build the user prompt for generating tests for one source file."""
    source_code = source_code[:max_source_chars]
    existing = existing_test_content[:max_existing_test_chars]
    framework = test_framework or env.test_framework or "unknown (use the most common one)"
    target_name = target_test_file or (
        gap.existing_test_file.name if gap.existing_test_file else ""
    )

    parts: list[str] = []
    essential: set[int] = set()
    parts.append(
        f"Project environment:\n"
        f"- Language: {env.language_name or env.language or 'unknown'}\n"
        f"- Test framework: {framework}\n"
        f"- Mocking libraries: {', '.join(env.mocking_libraries) or 'none detected'}\n"
    )
    essential.add(0)
    if file_map:
        parts.append(file_map)
        essential.add(len(parts) - 1)
    if target_name:
        parts.append(
            f"Target test file: `{target_name}`\n"
            f"Generate tests to be inserted into `{target_name}`. "
            f"Match `{framework}` syntax and conventions exactly."
        )
    parts.append("Project test conventions (FOLLOW THESE EXACTLY):\n" + conventions.to_prompt_text())
    if conventions.example_test:
        example = conventions.example_test[:max_existing_test_chars]
        parts.append(
            "Representative existing test from this project (match its style):\n"
            "```\n" + example + "\n```"
        )
    ut_library = ""
    if ut_knowledge is not None and not ut_knowledge.is_empty():
        ut_library = ut_knowledge.to_prompt_text(
            skip_examples=(conventions.example_test,) if conventions.example_test else (),
            max_chars=max_ut_library_chars,
        )
    if ut_library:
        folder = (
            f"`{ut_knowledge.test_dir.name}`"
            if ut_knowledge is not None and ut_knowledge.test_dir is not None
            else "the project's UT folder"
        )
        parts.append(
            f"Existing unit-test library learned from {folder} "
            f"({ut_knowledge.test_file_count} test files, "
            f"{ut_knowledge.test_case_count} test cases).\n"
            "STUDY IT AND IMITATE IT. It shows how this team writes tests: which "
            "fixtures, mocks, helpers and constants exist, how they declare their "
            "members, how they create and initialize every variable, collaborator, "
            "adapter and mock, and how they call the shared helpers.\n"
            "REUSE what already exists: use these fixtures, mocks, helper functions and "
            "constants with their exact names and signatures instead of writing new ones, "
            "and never redefine something that is already declared here.\n\n"
            + ut_library
        )
    parts.append(f"Source file under test ({source_name}):\n```\n{source_code}\n```")
    essential.add(len(parts) - 1)
    for name, content in related_files or []:
        parts.append(
            f"Related project file `{name}` (declares the API under test; "
            f"use it to write correct, compilable tests):\n```\n"
            + content[:max_source_chars]
            + "\n```"
        )
    if api_context:
        parts.append(
            "Project API context - everything the code under test is connected to: the "
            "variables, constants, enums, types, collaborators (readers, writers, "
            "publishers, subscribers, listeners, managers, queues) and function "
            "signatures declared in the files it depends on. Function bodies were removed; "
            "only declarations are shown.\n"
            "Use ONLY these exact names, types, enum values and signatures. Never invent a "
            "type, member, enum value, constant or overload that does not appear here or in "
            "the source shown above.\n\n" + api_context
        )
    if repo_context:
        source_note = (
            f" (full index: `{repo_knowledge_file}`)" if repo_knowledge_file else ""
        )
        parts.append(
            f"Repository knowledge{source_note} - the real symbols declared anywhere in "
            "this repository that are relevant to this source file: structs, enums with "
            "their exact enumerator names, type aliases, constants, macros, free "
            "functions and collaborator classes (readers, writers, publishers, "
            "subscribers, listeners, handlers, managers, factories, queues, ...).\n"
            "This is the ground truth for the whole project. Before you write a type, a "
            "member, an enum value or a constant, check that it appears here (or in the "
            "source shown above) and spell it exactly as listed. If what you need is not "
            "listed, do not invent it: use something that is listed, or write your best "
            "guess with a `TODO: ` comment on the line directly above saying what is "
            "missing.\n"
            "Members prefixed with `[protected]` or `[private]` really exist and are part "
            "of the behaviour you must cover - reach them with the access techniques "
            "described in this prompt instead of ignoring them.\n"
            "Where a type shows 'this project creates and initializes it like this', that "
            "snippet is real code taken from the repository: build that object the same "
            "way in the tests (same constructor or factory/manager call, same arguments, "
            "same order of initialization calls) instead of inventing your own.\n\n"
            + repo_context
        )
    if knowledge_docs:
        parts.append(knowledge_docs)
    if construction_notes:
        parts.append(construction_notes)
    if access_notes:
        parts.append(access_notes)
    if existing:
        parts.append(
            "The existing test file for this source (do NOT duplicate any of these tests):\n"
            "```\n" + existing + "\n```"
        )
    if gap.kind == GAP_ADDITIONAL_CASES:
        block = (
            f"Testing gap: {gap.reason}.\n"
            "This source file is ALREADY covered by the test file above. Your job is to "
            "propose ADDITIONAL test cases that will be appended to that existing test file.\n"
            "- Do NOT rewrite, restate or duplicate any existing test.\n"
            "- Target scenarios the existing tests miss: boundary values, empty/invalid "
            "inputs, error and exception paths, unusual argument combinations, state "
            "transitions and regression risks.\n"
            "- Use test names that do not collide with the existing ones."
        )
        if gap.untested_functions:
            block += (
                "\n- Concentrate on these symbols: "
                + ", ".join(gap.untested_functions[:12])
                + "."
            )
        parts.append(block)
    else:
        focus = ", ".join(gap.untested_functions[:12]) or "the public API of this file"
        parts.append(
            f"Testing gap: {gap.reason}.\n"
            f"Focus on these untested functions: {focus}."
        )
    if gap.focus_only and gap.untested_functions:
        parts[-1] += (
            "\nThe user selected these symbols explicitly: write tests ONLY for "
            + ", ".join(gap.untested_functions[:20])
            + ". Do not write tests for any other symbol of this file."
        )
    essential.add(len(parts) - 1)
    fixture = ""
    if header_file_name and header_template:
        fixture = _fixture_class_name(header_template)
        parts.append(_header_prompt_section(header_file_name, header_template, fixture))
        essential.add(len(parts) - 1)
    wants_fixture_block = bool(fixture_template.strip() and target_name)
    if wants_fixture_block:
        parts.append(
            _fixture_prompt_section(
                target_name, header_file_name, fixture, fixture_template
            )
        )
        essential.add(len(parts) - 1)
    parts.append(
        f"Write up to {max_tests} new unit tests. Requirements:\n"
        f"- Each test must be complete and self-contained (compilable as-is when inserted).\n"
        f"- Cover missing scenarios and relevant edge cases; prefer edge cases.\n"
        f"- Do NOT include imports/includes or file-level boilerplate inside the test blocks; "
        f"only the test function/method itself (plus any small helper it needs).\n"
        + (
            f"- Write every test as TEST_F({fixture}, <TestName>) so it uses the fixture "
            f"declared in `{header_file_name}`.\n"
            f"- Use the fixture members built by the fixture's own lifecycle (its "
            f"constructor and/or SetUp(), whichever this project uses) instead of "
            f"constructing the object under test again inside the test; never re-declare "
            f"there anything already declared in `{header_file_name}`.\n"
            f"- Call only methods that really exist in the code shown to you, with their "
            f"exact signatures and argument types.\n"
            if fixture
            else ""
        )
        + f"- Do NOT repeat existing tests.\n"
        + (
            "- Write the tests for the files listed under 'Files for this task' and for "
            "nothing else: every function you call, every type you instantiate and every "
            "value you assert must come from those files as they are quoted below (or "
            "from the files you opened at those paths). Do not test an imagined API and "
            "do not invent a file that is not listed there.\n"
            if file_map
            else ""
        )
        + (
            "- Reuse the existing UT library shown above: call its helper functions, derive "
            "from its fixtures, use its mocks and constants, and follow the same test "
            "structure. Only write a new helper when nothing suitable exists.\n"
            "- Create every variable, object, collaborator, adapter and mock the way the "
            "existing tests do it: copy their declaration style, their construction call "
            "(constructor, factory, helper, make_unique, mock builder), their argument "
            "shapes and the follow-up initialization calls shown in the setup/fixture "
            "bodies and construction recipes above. Do not invent a different way to build "
            "something the UT folder already builds.\n"
            if ut_library
            else ""
        )
        + (
            "- Non-public members are still your job: follow the access rules listed above "
            "and pick the technique that makes the member reachable (test-accessor "
            "subclass for protected members, an existing `friend`/`FRIEND_TEST` name for "
            "private ones, the `#define private public` include shim in the UT header when "
            "there is no friend, or the public API that exercises the member). Never skip "
            "a member, never leave a test body empty and never write a `TODO` saying a "
            "member is protected or private - work the access out and write the real test.\n"
            if access_notes
            else ""
        )
        + (
            "- Build inputs, expected values and collaborators only from the declarations "
            "listed in the Project API context: use the real enum values, constants, types "
            "and function signatures shown there, and create readers, publishers and other "
            "collaborators exactly the way that code declares/creates them.\n"
            if api_context
            else ""
        )
        + (
            "- Use the Repository knowledge section as the single source of truth for "
            "names: enum values, struct fields, class members, constants and collaborator "
            "types must be copied from there character by character. Never invent an enum "
            "value, a field, a reader/publisher/manager class or a constant that is not "
            "listed there or in the source under test.\n"
            "- Create every reader, writer, publisher, subscriber, manager, queue or other "
            "collaborator exactly the way the repository does it: copy the initialization "
            "snippet shown for that type (constructor or factory/manager call, its "
            "arguments, and the follow-up init/configure/subscribe calls). Never skip a "
            "required initialization step and never replace it with an invented one.\n"
            if repo_context
            else ""
        )
        + (
            "- Construction is mandatory-checked: every object you create must use one of "
            "the constructor signatures listed in 'Constructors that actually exist', with "
            "exactly that argument count and those argument types. A type that is not listed "
            "there and has no constructor in the code above must be default-constructed or "
            "obtained from the project's factory/manager - never call a constructor you did "
            "not see.\n"
            if construction_notes
            else ""
        )
        + _dependency_rule(header_file_name)
        + f"- Type correctness is mandatory: initialize every variable with a value of its "
        f"declared type, pass arguments of the exact parameter types, and compare against "
        f"expected values of the same type as the return type. Never assign a value of a "
        f"different type to a variable, and never reuse the same variable for values of "
        f"different types. Do not rely on implicit conversions (e.g. string vs number, "
        f"int vs float, signed vs unsigned, None/null vs a concrete object); use an "
        f"explicit, correctly typed literal or cast instead.\n"
        f"- Never declare something as null and then read from it. Only raw pointers and "
        f"smart pointers may be initialized with `nullptr`/`NULL`/`None`; writing "
        f"`Type obj = nullptr;` for a value, reference, container or string type is a "
        f"compilation error. Declare value objects plainly (`Type obj;`) or with a real, "
        f"correctly typed initializer, and assign a real object to any pointer before the "
        f"test dereferences it or calls a member on it.\n"
        f"- Place English comments directly ABOVE each test function header (use # for Python, "
        f"// for other languages) using exactly these labels:\n"
        f"  # explenation : <one sentence describing what the test verifies>\n"
        f"  # Input Params : <concrete input values>\n"
        f"  # Expected Result : <expected return value or behavior>\n"
        f"  Example (Python):\n"
        f"  # explenation : Verifies that add returns the sum of two positive integers.\n"
        f"  # Input Params : a=1, b=2\n"
        f"  # Expected Result : 3\n"
        f"  def test_add_positive_integers():\n"
        f"      assert add(1, 2) == 3\n\n"
        f"Output format - for EACH test output exactly:\n"
        f"{TEST_START} <test_name> | <edge_case:yes/no> | <description> | "
        f"<input params> | <expected output>\n"
        f"<the test code (including the English comments above)>\n"
        f"{TEST_END}\n"
        + (
            f"Output the {UT_HEADER_START} block first"
            + (f", then the {UT_FIXTURE_START} block" if wants_fixture_block else "")
            + ", then the test blocks.\n"
            if header_file_name and header_template
            else (
                f"Output the {UT_FIXTURE_START} block first, then the test blocks.\n"
                if wants_fixture_block
                else ""
            )
        )
        + f"Output nothing else."
    )
    essential.add(len(parts) - 1)
    result = "\n\n".join(parts)
    if max_total_chars is not None and len(result) > max_total_chars:
        result = _fit_to_budget(parts, essential, max_total_chars)
    return result


def _fit_to_budget(parts: list[str], essential: set[int], budget: int) -> str:
    """Drop whole context sections - never the instructions the model must follow."""
    keep = {index for index in essential if 0 <= index < len(parts)}
    used = sum(len(parts[index]) + 2 for index in keep)
    dropped = 0
    for index, part in enumerate(parts):
        if index in keep:
            continue
        cost = len(part) + 2
        if used + cost > budget:
            dropped += 1
            continue
        keep.add(index)
        used += cost
    kept = [parts[index] for index in sorted(keep)]
    if dropped:
        kept.append(
            f"[{dropped} context section(s) were omitted to fit the model's context "
            "window. Work with what is shown above and mark anything you cannot verify "
            "with a `TODO: ` comment.]"
        )
    return "\n\n".join(kept)


def _fixture_class_name(header_template: str) -> str:
    match = re.search(r"^\s*class\s+(\w+)\s*:", header_template, re.MULTILINE)
    return match.group(1) if match else ""


def _dependency_rule(header_file_name: str) -> str:
    """Rule forcing every used symbol to be reachable through an include/import."""
    where = (
        f"add the missing `#include` to `{header_file_name}` (the test file includes it)"
        if header_file_name
        else "add the missing import/include at the top of the test file"
    )
    return (
        "- Never use a type, constant, enum, macro, helper or function whose declaring file "
        "is not included/imported. For EVERY symbol you write that is not declared in the "
        f"source file under test, {where}, using the exact include/import line listed for "
        "that file above. If you cannot find the file that declares a symbol, do not use "
        "that symbol - use one you can see, or write your best guess with a `TODO: ` comment "
        "on the line directly above naming the file you are missing.\n"
    )


def _header_prompt_section(header_file_name: str, header_template: str, fixture: str) -> str:
    lines = [
        f"Companion unit-test header: `{header_file_name}`.",
        "The test file includes this header, so every declaration the tests need "
        "(fixture members, mocks, helpers, constants) must be declared there.",
        "Starting skeleton - keep the include guard and ALL of its #include lines, "
        "and extend it as needed:",
        "```\n" + header_template.strip() + "\n```",
        "Before writing it, read the source and its header carefully and derive the fixture "
        "from the real code:\n"
        "- Look at the CONSTRUCTOR of the class under test: its parameters, the objects it "
        "requires and the order in which it initializes them. SetUp() must build the object "
        "under test exactly the way the constructor requires (same arguments, same order, "
        "same initialization steps), and TearDown() must release what SetUp() created.\n"
        "- Look at every collaborator the class uses - readers, writers, publishers, "
        "subscribers, listeners, handlers, managers, queues - and declare a member of the "
        "correct type for each one the tests need, with the exact type, template arguments "
        "and namespace used in the code. Prefer the project's existing fakes/mocks "
        "(e.g. from `ManagersForUT.h` / `TestEnvironment.h`) over inventing new ones.\n"
        "- READERS and PUBLISHERS specifically: study how the production code (and any "
        "existing test) declares and creates them, and reproduce that exactly.\n"
        "  * If the code only DECLARES them (member/field, no construction), declare them "
        "the same way in the fixture.\n"
        "  * If the code only INITIALIZES/creates them (factory call, `create_datareader` / "
        "`create_datawriter` / `create_publisher` / `create_subscriber`, participant or "
        "manager call, registration/attach), reproduce that creation in SetUp() with the "
        "same factory, arguments, topic/type names and QoS, and undo it in TearDown().\n"
        "  * If the code does BOTH, do both: declare the member in the header and "
        "initialize it in SetUp() exactly as the production code does.\n"
        "  * Use the same type, template/type parameters, namespace, ownership (raw pointer, "
        "smart pointer, value) and topic/type names that appear in the code.\n"
        "- Declare the object under test as a member (by value, pointer or smart pointer, "
        "matching how the production code owns it) so every test can use it.\n"
        "- NEVER initialize a member with `nullptr`/`NULL` unless its type is a raw pointer "
        "or a smart pointer: `Type obj = nullptr;` on a value, reference, container or "
        "string member does not compile. Declare value members plainly (`Type obj;`) or "
        "with a real, correctly typed initializer.\n"
        "- Every member the tests read, dereference or call methods on must actually be "
        "created in SetUp() (constructor call, `new`, `std::make_unique`, factory, manager "
        "call...). Never declare a member as null, leave SetUp() without creating it, and "
        "then use it in a test - and never declare a reference member you cannot bind.\n"
        "- If the skeleton contains a test-accessor class (a class deriving from the class "
        "under test that re-exports its protected members with `using`), KEEP it and extend "
        "it with any other protected member the tests need; declare the object under test "
        "with that accessor type when the tests exercise protected members.\n"
        "- PRIVATE members the tests must reach: if the class under test already declares a "
        "`friend class X;` or `FRIEND_TEST(Suite, Case)`, name the fixture/test exactly like "
        "that friend. Otherwise put the access shim at the very TOP of this header, before "
        "every other include:\n"
        "  ```\n"
        "  #define private public\n"
        "  #define protected public\n"
        '  #include "<header of the class under test>"\n'
        "  #undef protected\n"
        "  #undef private\n"
        "  ```\n"
        "  and then call the private members directly. Never modify the production class, "
        "and never drop a test because a member is not public.\n"
        "- Do not invent types, headers, methods or constructor signatures that do not "
        "appear in the code shown to you.\n"
        "- INCLUDES: this header must `#include` every file that declares something the "
        "fixture members or the tests use. Go over every type, constant, enum, macro and "
        "helper you write, find the file it is declared in (see the include list and the "
        "API context above) and make sure the exact include line for that file is present. "
        "Keep the skeleton's includes, add the missing ones, and never use a symbol whose "
        "file you did not include.\n"
        "- CONSTRUCTION: build every object with a constructor that really exists in the "
        "code shown to you - same argument count, same argument types, same order. If the "
        "class under test has no visible constructor, declare it plainly (`Type obj;`) or "
        "create it the way the production code does; never invent constructor parameters.\n"

        "- If you cannot determine from the code shown to you how a reader, publisher or any "
        "other member should be declared or initialized, still write your best-guess line, "
        "and put a comment starting with `TODO: ` on its OWN line directly ABOVE that line "
        "(`// TODO: ...` in C/C++, `# TODO: ...` in Python) describing exactly what is "
        "missing and what has to be done, so a human can finish it "
        "(e.g. `// TODO: search for the right reader and initiate`). "
        "Use this marker only where you are genuinely unsure - never guess silently. "
        "A member being protected, private, internal or underscore-prefixed is NOT a "
        "reason for a `TODO`: resolve the access with the techniques above and write the "
        "real code.",
        f"Output the COMPLETE, compilable content of `{header_file_name}` "
        "(not a diff, not a fragment) between these markers:",
        f"{UT_HEADER_START}\n<full content of {header_file_name}>\n{UT_HEADER_END}",
        "The header must contain no test cases - only includes, the fixture class "
        "and any declarations the tests rely on.",
    ]
    if fixture:
        lines.append(
            f"Keep the fixture class named `{fixture}`, and make sure the test file and this "
            "header fit together: every member, mock and helper used by the tests is declared "
            "here, and every test uses them exactly as declared."
        )
    return "\n".join(lines)


def parse_ut_header_response(response: str) -> tuple[str, str]:
    """Extract the generated UT header. Returns (header_content, response_without_it)."""
    match = re.search(
        re.escape(UT_HEADER_START) + r"\n?(.*?)" + re.escape(UT_HEADER_END),
        response,
        re.DOTALL,
    )
    if match is None:
        return "", response
    content = _strip_code_fences(match.group(1)).strip("\n")
    remainder = response[: match.start()] + response[match.end():]
    return content, remainder


def _fixture_prompt_section(
    test_file_name: str, header_file_name: str, fixture: str, fixture_template: str
) -> str:
    """Ask for the fixture's own lifecycle bodies, in the file this project puts them."""
    owner = f"`{fixture}`" if fixture else "the fixture"
    declared_in = f" declared in `{header_file_name}`" if header_file_name else ""
    return "\n".join(
        [
            f"Fixture implementation block of `{test_file_name}`.",
            f"This project writes the lifecycle of {owner} in the test source file, not "
            f"inline in the header. Here is the skeleton, generated from the members"
            f"{declared_in} - keep every signature exactly as written and fill in the "
            "bodies:",
            "```\n" + fixture_template.strip() + "\n```",
            "Fill those bodies the way the existing unit tests do it (see the fixture "
            "lifecycle, setup and construction snippets above): create every member the "
            "tests use with the same constructor/factory/helper calls, the same arguments "
            "and the same order of initialization steps, and release in the "
            "destructor/TearDown exactly what the constructor/SetUp created.",
            "- Never leave a body empty while the fixture declares a member the tests use.\n"
            "- Only construct objects with constructors that really exist in the code shown "
            "to you - same argument count, same argument types.\n"
            "- Never initialize a value, reference, container or string member with "
            "`nullptr`/`NULL`; only raw and smart pointers may hold null.\n"
            "- Add no test cases here, and do not redeclare anything the header already "
            "declares.\n"
            "- If you cannot work out how a member must be created, still write your "
            "best-guess line and put a `// TODO: ` comment on its own line directly ABOVE "
            "it saying exactly what is missing.",
            f"Output the COMPLETE top of `{test_file_name}` (its include lines plus these "
            "definitions, nothing else) between these markers:",
            f"{UT_FIXTURE_START}\n<include lines and fixture definitions>\n{UT_FIXTURE_END}",
        ]
    )


def parse_ut_fixture_response(response: str) -> tuple[str, str]:
    """Extract the generated fixture block. Returns (fixture_code, rest of response)."""
    match = re.search(
        re.escape(UT_FIXTURE_START) + r"\n?(.*?)" + re.escape(UT_FIXTURE_END),
        response,
        re.DOTALL,
    )
    if match is None:
        return "", response
    content = _strip_code_fences(match.group(1)).strip("\n")
    remainder = response[: match.start()] + response[match.end():]
    return content, remainder


# --- response parsing -------------------------------------------------------
# Hard cap on how many tests one reply may yield through the loose fallbacks.
_MAX_EXTRACTED_TESTS = 50
# How far after a test signature the opening brace of its body may sit.
_MAX_SIGNATURE_TO_BODY_CHARS = 400

_THINK_BLOCK_RE = re.compile(
    r"<(think|thinking|reasoning|reflection)>.*?</\1>", re.DOTALL | re.IGNORECASE
)
_THINK_OPEN_RE = re.compile(
    r"<(?:think|thinking|reasoning|reflection)>", re.IGNORECASE
)
# Unterminated fences are accepted: a reply cut off by the token limit still
# carries usable tests.
_FENCE_RE = re.compile(r"```[^\n`]*\n(.*?)(?:```|\Z)", re.DOTALL)

# "=== TEST: name | ...", "## Test 2: name", "TEST - name", ...
_TEST_START_RE = re.compile(
    r"^[ \t]*(?:[=\-*#~_]{2,}[ \t]*)?(?:UNIT[ _]?)?TEST(?:[ _]?CASE)?"
    r"[ \t]*\d*[ \t]*[:\-][ \t]*(?P<header>[^\n]*)$",
    re.IGNORECASE | re.MULTILINE,
)
# "=== END TEST ===", "--- end test case ---", "END TEST", ...
_TEST_END_RE = re.compile(
    r"^[ \t]*(?:[=\-*#~_]{2,}[ \t]*)?END[ \t]*(?:OF[ \t]+)?TEST(?:[ _]?CASE)?"
    r"[ \t]*(?:[=\-*#~_]{2,})?[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)

_IDENTIFIER_RE = re.compile(r"[A-Za-z_]\w*")
_CODE_HINT_RE = re.compile(
    r"(?m)^[ \t]*(?:#include|import\s|from\s|using\s|namespace\s|package\s|def\s"
    r"|class\s|struct\s|@|\[|public\s|private\s|internal\s|void\s|func\s|TEST"
    r"|it\(|test\()|[;{}]|\bassert\b|EXPECT_|ASSERT_|REQUIRE\(|CHECK\("
)
_ASSERTION_RE = re.compile(
    r"\bassert\w*\s*[\(( ]|EXPECT_\w+|ASSERT_\w+|REQUIRE\s*\(|CHECK\s*\("
    r"|Assert\.\w+|\bexpect\s*\("
)

_PY_FUNC_RE = re.compile(
    r"(?m)^(?:[ \t]*(?:@[\w.]+(?:\([^\n)]*\))?|#[^\n]*)[ \t]*\n)*"
    r"[ \t]*(?:async[ \t]+)?def[ \t]+test\w*[ \t]*\([^\n]*\)[^\n:]*:"
    r"(?:\n(?:[ \t]+[^\n]*|[ \t]*))*"
)
_PY_CLASS_RE = re.compile(
    r"(?m)^(?:[ \t]*(?:@[\w.]+(?:\([^\n)]*\))?|#[^\n]*)[ \t]*\n)*"
    r"[ \t]*class[ \t]+\w*[Tt]est\w*[^\n]*:"
    r"(?:\n(?:[ \t]+[^\n]*|[ \t]*))*"
)

_CPP_TEST_RE = re.compile(
    r"(?m)^[ \t]*(?:TYPED_TEST_P|TYPED_TEST|TEST_CASE_METHOD|TEST_CASE|TEST_METHOD"
    r"|BOOST_FIXTURE_TEST_CASE|BOOST_AUTO_TEST_CASE|SCENARIO|TEST_F|TEST_P|TEST)"
    r"[ \t]*\("
)
_GO_TEST_RE = re.compile(
    r"(?m)^[ \t]*func[ \t]+(?:\([^)\n]*\)[ \t]*)?"
    r"(?:Test|Benchmark|Fuzz|Example)\w*[ \t]*\("
)
_JS_TEST_RE = re.compile(r"(?m)^[ \t]*(?:it|test)(?:\.\w+)?[ \t]*\(")
_JAVA_TEST_RE = re.compile(
    r"(?m)^[ \t]*(?:@(?:Test|ParameterizedTest|RepeatedTest)\b"
    r"|(?:public|protected|private)[^\n;=]*\bvoid[ \t]+\w+[ \t]*\([^)\n]*\)[^\n{;]*\{)"
)
_CSHARP_TEST_RE = re.compile(
    r"(?m)^[ \t]*(?:\[(?:Fact|Theory|Test|TestMethod|TestCase)[^\]\n]*\]"
    r"|(?:public|internal)[^\n;=]*\bvoid[ \t]+\w+[ \t]*\([^)\n]*\)[^\n{;]*\{)"
)

_TEST_SIGNATURES: dict[str, tuple[re.Pattern[str], ...]] = {
    "cpp": (_CPP_TEST_RE,),
    "c": (_CPP_TEST_RE,),
    "java": (_JAVA_TEST_RE,),
    "csharp": (_CSHARP_TEST_RE,),
    "go": (_GO_TEST_RE,),
    "javascript": (_JS_TEST_RE,),
    "typescript": (_JS_TEST_RE,),
}
_ALL_SIGNATURES = (
    _CPP_TEST_RE,
    _GO_TEST_RE,
    _JAVA_TEST_RE,
    _CSHARP_TEST_RE,
    _JS_TEST_RE,
)


def parse_generation_response(
    response: str,
    *,
    language: str = "python",
) -> list[tuple[str, str, bool, str]]:
    """Parse LLM output into (name, code, is_edge_case, rationale) tuples.

    Models - especially small local ones - rarely reproduce the requested
    ``=== TEST: ... === END TEST ===`` markers exactly. The parser therefore
    works through progressively looser strategies and only gives up when the
    reply really contains no test code at all:

    1. marker blocks, accepting the common marker spellings;
    2. real test functions found inside fenced code blocks;
    3. real test functions found in the raw reply (no fences, no markers);
    4. whole fenced code blocks;
    5. the whole reply, when it is clearly test code.
    """
    text = _clean_response(response)
    if not text.strip():
        return []

    results = _tests_from_marker_blocks(text, language)
    if results:
        return results

    fenced = [block for block in _fenced_blocks(text) if block.strip()]
    for chunk in fenced or [text]:
        results.extend(
            (name, code, False, "")
            for name, code in extract_test_functions(chunk, language)
        )
    if not results and fenced:
        # Fences were present but held no recognizable function: search the
        # whole reply anyway (models often fence only part of the answer).
        results.extend(
            (name, code, False, "")
            for name, code in extract_test_functions(text, language)
        )
    if results:
        return _dedupe_tests(results)

    # Fallback: fenced code blocks without any recognizable test signature.
    for block in fenced:
        code = block.strip("\n")
        if _looks_like_code(code):
            name = _guess_test_name(code) or f"generated_test_{len(results) + 1}"
            results.append((name, code, False, ""))
    if results:
        return _dedupe_tests(results)

    # Last resort: the model answered with plain, unfenced test code.
    code = _strip_code_fences(text).strip("\n")
    if _looks_like_test_code(code):
        results.append((_guess_test_name(code) or "generated_test_1", code, False, ""))
    return results


def response_has_tests(text: str, *, language: str = "") -> bool:
    """True when ``text`` carries something the generator can turn into tests."""
    return bool(parse_generation_response(text, language=language))


def extract_test_functions(text: str, language: str) -> list[tuple[str, str]]:
    """Find real test functions in ``text``. Returns ``(name, code)`` pairs."""
    lang = (language or "").strip().lower()
    if lang == "python":
        return _python_test_functions(text)
    patterns = _TEST_SIGNATURES.get(lang)
    found = _brace_test_functions(text, patterns or _ALL_SIGNATURES)
    if not found and patterns is None:
        found = _python_test_functions(text)
    return found


def _clean_response(response: str) -> str:
    """Drop thinking spans that leaked into the answer channel."""
    if not response:
        return ""
    text = _THINK_BLOCK_RE.sub("", response)
    # An unterminated <think> ... swallows the rest of the reply; keep what is
    # before it only when that part carries content.
    open_tag = _THINK_OPEN_RE.search(text)
    if open_tag is not None:
        head = text[: open_tag.start()]
        tail = text[open_tag.end():]
        text = head if head.strip() else tail
    return text


def _tests_from_marker_blocks(
    text: str, language: str
) -> list[tuple[str, str, bool, str]]:
    """Parse ``=== TEST: ... === END TEST ===`` style blocks, spelling-tolerant."""
    results: list[tuple[str, str, bool, str]] = []
    starts = list(_TEST_START_RE.finditer(text))
    for index, match in enumerate(starts):
        body_start = match.end()
        limit = starts[index + 1].start() if index + 1 < len(starts) else len(text)
        end = _TEST_END_RE.search(text, body_start, limit)
        body = text[body_start: end.start() if end is not None else limit]
        code = _code_from_block(body)
        if not _looks_like_code(code):
            continue
        name, edge, rationale, input_params, expected_output = _parse_header(
            _clean_marker_header(match.group("header"))
        )
        code = ensure_english_test_comments(
            code,
            description=rationale,
            input_params=input_params,
            expected_output=expected_output,
            language=language,
        )
        results.append((_resolve_name(name, code, len(results) + 1), code, edge, rationale))
    return _dedupe_tests(results)


def _code_from_block(body: str) -> str:
    """Return the code of one marker block, dropping surrounding prose."""
    fenced = [block for block in _fenced_blocks(body) if block.strip()]
    if fenced:
        return "\n\n".join(block.strip("\n") for block in fenced)
    return _strip_code_fences(body).strip("\n")


def _fenced_blocks(text: str) -> list[str]:
    return [match.group(1) for match in _FENCE_RE.finditer(text)]


def _clean_marker_header(header: str) -> str:
    return header.strip().strip("=-#*~_ \t").strip()


def _resolve_name(name: str, code: str, index: int) -> str:
    if _IDENTIFIER_RE.fullmatch(name or ""):
        return name
    guessed = _guess_test_name(code)
    if guessed:
        return guessed
    cleaned = re.sub(r"\W+", "_", name or "").strip("_")
    return cleaned or f"generated_test_{index}"


def _dedupe_tests(
    tests: list[tuple[str, str, bool, str]]
) -> list[tuple[str, str, bool, str]]:
    seen: set[str] = set()
    unique: list[tuple[str, str, bool, str]] = []
    for name, code, edge, rationale in tests:
        key = re.sub(r"\s+", " ", code).strip()
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append((name, code, edge, rationale))
    return unique


def _looks_like_code(text: str) -> bool:
    return bool(text.strip()) and bool(_CODE_HINT_RE.search(text))


def _looks_like_test_code(text: str) -> bool:
    """Stricter than ``_looks_like_code``: an assertion or a test signature."""
    if not text.strip():
        return False
    if _ASSERTION_RE.search(text):
        return True
    return any(pattern.search(text) for pattern in _ALL_SIGNATURES)


def _python_test_functions(text: str) -> list[tuple[str, str]]:
    """Top-level ``def test_*`` functions and ``class Test*`` suites."""
    results: list[tuple[str, str]] = []
    consumed = 0
    matches = sorted(
        list(_PY_CLASS_RE.finditer(text)) + list(_PY_FUNC_RE.finditer(text)),
        key=lambda match: match.start(),
    )
    for match in matches:
        if match.start() < consumed:
            continue
        code = match.group(0).strip("\n")
        consumed = match.end()
        if not code.strip():
            continue
        results.append(
            (_guess_test_name(code) or f"generated_test_{len(results) + 1}", code)
        )
        if len(results) >= _MAX_EXTRACTED_TESTS:
            break
    return results


def _brace_test_functions(
    text: str, patterns: tuple[re.Pattern[str], ...]
) -> list[tuple[str, str]]:
    """Brace-matched test functions for C-like languages."""
    starts: set[int] = set()
    for pattern in patterns:
        starts.update(match.start() for match in pattern.finditer(text))
    results: list[tuple[str, str]] = []
    consumed = 0
    for start in sorted(starts):
        if start < consumed:
            continue
        extent = _block_extent(text, start)
        if extent is None:
            continue
        end, unclosed = extent
        block_start = max(_block_start_with_comments(text, start), consumed)
        code = text[block_start:end].strip("\n")
        consumed = end
        if not code.strip():
            continue
        if unclosed:
            # The reply was cut off by the output-token limit: close the test so
            # it can still be reviewed, and say so in a TODO.
            code = (
                code.rstrip()
                + "\n    // TODO: the model's reply was cut off here; "
                "complete this test.\n"
                + "\n".join("}" for _ in range(unclosed))
            )
        results.append(
            (_guess_test_name(code) or f"generated_test_{len(results) + 1}", code)
        )
        if len(results) >= _MAX_EXTRACTED_TESTS:
            break
    return results


def _block_extent(text: str, start: int) -> tuple[int, int] | None:
    """``(end index, unclosed braces)`` for the block opened after ``start``."""
    index = text.find("{", start)
    # A signature mentioned in prose has no body right after it; do not let the
    # scanner jump into unrelated code far below.
    if index < 0 or index - start > _MAX_SIGNATURE_TO_BODY_CHARS:
        return None
    depth = 0
    position = index
    length = len(text)
    while position < length:
        char = text[position]
        if char in "\"'":
            position = _skip_string(text, position)
            continue
        if char == "/" and position + 1 < length:
            following = text[position + 1]
            if following == "/":
                newline = text.find("\n", position)
                position = length if newline < 0 else newline + 1
                continue
            if following == "*":
                closing = text.find("*/", position + 2)
                position = length if closing < 0 else closing + 2
                continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                end = position + 1
                while end < length and text[end] in ");, \t":
                    end += 1
                return end, 0
        position += 1
    return length, max(depth, 1)


def _skip_string(text: str, index: int) -> int:
    quote = text[index]
    position = index + 1
    length = len(text)
    while position < length:
        char = text[position]
        if char == "\\":
            position += 2
            continue
        if char == quote or char == "\n":
            return position + 1
        position += 1
    return length


def _block_start_with_comments(text: str, start: int) -> int:
    """Include the comment/annotation lines written directly above the test."""
    line_start = text.rfind("\n", 0, start) + 1
    lines = text[:line_start].split("\n")
    index = len(lines) - 1
    while index > 0 and _is_attached_prefix_line(lines[index - 1]):
        index -= 1
    return sum(len(line) + 1 for line in lines[:index])


def _is_attached_prefix_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    return stripped.startswith(("//", "#", "/*", "*", "@", "["))


def _parse_header(header: str) -> tuple[str, bool, str, str, str]:
    parts = [p.strip() for p in header.split("|")]
    name = parts[0].strip(" =:") or "generated_test"
    edge = len(parts) > 1 and "yes" in parts[1].lower()
    rationale = parts[2] if len(parts) > 2 else ""
    input_params = parts[3] if len(parts) > 3 else ""
    expected_output = parts[4] if len(parts) > 4 else ""
    return name, edge, rationale, input_params, expected_output


def ensure_english_test_comments(
    code: str,
    *,
    description: str,
    input_params: str,
    expected_output: str,
    language: str,
) -> str:
    """Add English Input/Output/Description comments when the model omitted them."""
    if _has_documentation_comments(code):
        return code
    comment_lines = _comment_lines(
        description=description,
        input_params=input_params,
        expected_output=expected_output,
        language=language,
    )
    if not comment_lines:
        return code
    return _insert_comments_before_signature(code, comment_lines)


def _comment_prefix(language: str) -> str:
    return "#" if language == "python" else "//"


def _comment_lines(
    *,
    description: str,
    input_params: str,
    expected_output: str,
    language: str,
) -> list[str]:
    prefix = _comment_prefix(language)
    lines: list[str] = []
    if description:
        lines.append(f"{prefix} explenation : {description}")
    if input_params:
        lines.append(f"{prefix} Input Params : {input_params}")
    if expected_output:
        lines.append(f"{prefix} Expected Result : {expected_output}")
    return lines


def _has_documentation_comments(code: str) -> bool:
    lowered = code.lower()
    has_input = "input params :" in lowered
    has_output = "expected result :" in lowered
    return has_input and has_output


def _insert_comments_before_signature(
    code: str,
    comment_lines: list[str],
) -> str:
    lines = code.splitlines()
    signature_idx = _find_signature_line_index(lines)
    if signature_idx is None:
        return code

    indent = lines[signature_idx][: len(lines[signature_idx]) - len(lines[signature_idx].lstrip())]
    indented = [f"{indent}{line}" for line in comment_lines]

    new_lines = lines[:signature_idx] + indented + lines[signature_idx:]
    return "\n".join(new_lines)


def _find_signature_line_index(lines: list[str]) -> int | None:
    signature_patterns = (
        r"^\s*def\s+\w+\s*\(",
        r"^\s*func\s+Test\w+\s*\(",
        r"^\s*TEST(?:_F|_P)?\s*\(",
        r"^\s*TEST_CASE\s*\(",
        r"^\s*(?:public\s+)?void\s+\w+\s*\(",
        r"^\s*(?:it|test)\s*\(",
    )
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("@") or stripped.startswith("#"):
            continue
        if any(re.match(pattern, line) for pattern in signature_patterns):
            return idx
    return None


def _strip_code_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```[a-zA-Z+]*\n", "", text)
    text = re.sub(r"\n```$", "", text)
    return text


def _guess_test_name(code: str) -> str | None:
    patterns = [
        r"\bdef\s+(test\w+)",                      # python
        r"\bTEST(?:_F|_P)?\s*\(\s*\w+\s*,\s*(\w+)",  # gtest
        r"TEST_CASE\s*\(\s*\"([^\"]+)\"",           # catch2
        r"\b(?:it|test)\s*\(\s*['\"`](.+?)['\"`]",   # js
        r"\bvoid\s+(\w*[Tt]est\w*)\s*\(",           # java/c#
        r"^func\s+(Test\w+)",                        # go
        r"\bvoid\s+(\w+)\s*\(",                     # java/c# method without "test"
    ]
    for pat in patterns:
        match = re.search(pat, code, re.MULTILINE)
        if match:
            return match.group(1)
    return None
