---
name: software-design-philosophy
description: Strategic software design, implementation, refactoring, code review, and performance engineering framework based on John Ousterhout's A Philosophy of Software Design. Use whenever designing new modules or APIs, writing production code, modifying or refactoring existing codebases, performing architecture or PR code reviews, diagnosing complexity or technical debt, or optimizing slow code, especially in Python, Rust, or Go.
---

# Software Design Philosophy

Reduce software complexity across the entire lifecycle: design, implementation, refactoring, code review, and performance optimization. Complexity is anything in a system's structure that makes it hard to understand or modify, caused by dependencies and obscurity, and manifesting as change amplification, cognitive load, and unknown unknowns.

## When to Use

- Designing a new class, module, package, service, or API before writing code
- Implementing features or bug fixes in Python, Rust, or Go
- Modifying, extending, or refactoring existing or legacy code
- Reviewing pull requests, auditing code quality, or diagnosing technical debt
- Optimizing performance without sacrificing modularity and readability

## Operating Modes

Identify which mode applies to the current task and follow its workflow. Most non-trivial coding tasks combine Mode 1 and Mode 2, or Mode 3 and Mode 4.

### Mode 1: Design and Architecture

Adopt a **strategic programming** mindset. Working code is not enough; the primary goal is a clean long-term structure that makes future changes easy. Invest roughly 10-20% extra thought up front to avoid compounding complexity.

1. **Design It Twice:** Before writing implementation code for any non-trivial module, API, or subsystem, sketch at least two radically different interface designs. Compare them on caller ergonomics, interface simplicity, generality, and performance. Pick the cleanest design or synthesize their best traits.

2. **Build Deep Modules:** Maximize functionality hidden behind the simplest possible interface. Prefer fewer, deeper modules over many tiny shallow classes ("classitis"). Make the common case automatic through sensible defaults so callers never have to configure or know about rare features.

3. **Hide Information by Knowledge, Not Time:** Encapsulate each design decision (file format, protocol parsing, caching policy, storage layout) inside a single module. Never decompose modules strictly by runtime execution order (temporal decomposition), because reading and writing or parsing and framing usually share the same knowledge.

4. **Make Interfaces Somewhat General-Purpose:** Implement only the functionality needed today, but shape the public interface to be general enough for multiple uses. Special-purpose APIs leak caller concepts into lower layers and proliferate shallow methods. Ask: "What is the simplest interface that covers all current needs?" and "In how many situations will this method be used?"

5. **Enforce Different Layer, Different Abstraction:** Each architectural layer must provide a genuinely different abstraction from the layers above and below it. If a method just forwards arguments to a lower layer with a similar signature, eliminate the pass-through layer by exposing the lower module directly, redistributing responsibilities, or merging the modules.

6. **Write Interface Comments First:** Before implementing method bodies, write the module docstring, public method signatures, and their interface docstrings (contract, parameters, return values, side effects, preconditions, exceptions). Use comments as a design canary: if an interface is hard to describe simply and completely, redesign the abstraction before coding.

### Mode 2: Implementation

1. **Pull Complexity Downwards:** When complexity is unavoidable and closely tied to a module's core responsibility, absorb it inside the module rather than pushing it onto callers via extra exceptions, awkward multi-step protocols, or unnecessary configuration parameters. Compute sensible defaults dynamically instead of exporting knobs callers cannot reliably tune.

2. **Define Errors and Special Cases Out of Existence:** Reduce the number of places where errors or edge cases must be handled, using this priority order:

   - **Redefine semantics** so the normal path handles edge cases cleanly with no exception (e.g., idempotent `delete`, range clamping, empty selection object instead of nullable `None` checks).
   - **Mask exceptions** at a low level when recovery belongs inside the subsystem (e.g., transparent retries for transient transport failures).
   - **Aggregate exceptions** at a high-level boundary (e.g., request dispatcher or middleware) so one handler converts domain errors into responses instead of duplicating `try/except` blocks everywhere.
   - **Crash fast** with clear diagnostics on unrecoverable internal invariants or fatal resource exhaustion where callers cannot meaningfully recover.

3. **Decide Better Together vs. Better Apart:** Combine code when pieces share hidden knowledge, simplify the resulting interface, eliminate repetition, or cannot be understood independently. Separate general-purpose mechanisms from special-purpose policy code (pull special-purpose code upwards, keep lower layers general). Never split a function solely because of an arbitrary line-count rule; a 60-line function with a simple signature and clear sequential phases is better than five conjoined 12-line helpers.

4. **Choose Precise, Consistent Names:** A name must create an accurate mental image in isolation. Avoid vague names (`data`, `result` in void functions, `status`, `count`, `block` for two different concepts). Use boolean predicates (`is_visible`, `has_pending_write`). Use the exact same name for a concept everywhere, never reuse it for another concept, and qualify variants with consistent prefixes (`src_block`, `dst_block`). If picking a clean name is hard, rethink the underlying design.

5. **Write Strategic Comments:** Comments must explain what is NOT obvious from the adjacent code. Never repeat the code or echo the identifier name.

   - **Lower-level comments** add precision to declarations: units, inclusive vs. exclusive bounds, meaning of `None`/empty, resource ownership, and invariants (describe what a variable represents as nouns, not how it is mutated).
   - **Higher-level comments** add intuition inside non-trivial logic: summarize what a major block or loop achieves and why ("how we get here"), not line-by-line mechanics.
   - **Interface comments** describe only what callers need; keep implementation details strictly inside the body.
   - **Cross-module decisions** live once in a central spot (e.g., a shared enum or `docs/design_notes.md`) with short cross-references from dependent code.

6. **Make Code Obvious:** Write for ease of reading, not ease of writing. Replace generic tuples/pairs/dicts with named types so field meanings are self-evident. Match declared and allocated types. Use blank lines and brief phase headers to visually separate logical blocks. Document any control flow that defies reader expectations (callbacks, event hooks, background threads/tasks).

### Mode 3: Modifying and Refactoring Existing Code

1. **Stay Strategic on Every Edit:** Resist the tactical urge to make the smallest patch that passes. Ask: "What would this design look like if I had built it from the start knowing about this requirement?" Leave the surrounding structure slightly cleaner than you found it, bounded by practical project constraints.

2. **Follow "When in Rome" Consistency:** Inspect surrounding files for naming, ordering, error handling, typing, and layout conventions, and match them strictly. Do not introduce a "better" local style unless the change is worth migrating the entire codebase consistently.

3. **Use Unit Tests to Enable Refactoring:** Rely on unit tests as a safety net for structural improvements. When fixing a bug, write a failing test reproducing the bug first, then fix it. When building new features, design the full abstraction cleanly rather than hacking incrementally to pass one tiny test at a time.

4. **Keep Comments Synchronized:** Keep comments physically close to the code they describe and never duplicate explanations across files. Put lasting rationale ("why this workaround exists") in code comments, not only in Git commit messages. Scan the full diff before finishing to verify no comment became stale.

### Mode 4: Code Review and Audit

When reviewing code, a diff, or an architecture, load `references/red-flags.md` and run a structured audit:

1. **Check Complexity Symptoms:** Identify change amplification, excessive cognitive load, and unknown unknowns.
2. **Scan for Red Flags:** Flag concrete issues by exact name (Shallow Module, Information Leakage, Temporal Decomposition, Overexposure, Pass-Through Method, Pass-Through Variable, Repetition, Special-General Mixture, Conjoined Methods, Comment Repeats Code, Implementation Contaminates Interface, Vague/Hard-to-Pick Name, Hard to Describe, Nonobvious Code).
3. **Propose Concrete Refactors:** For each high-impact finding, state why it increases complexity and show the cleaner interface or merged/separated structure. Prioritize design and abstraction issues over cosmetic nitpicks.

### Mode 5: Performance Engineering

Clean design and high performance reinforce each other: fewer shallow layers and fewer special-case branches run faster.

1. **Choose Naturally Efficient Defaults:** Avoid obvious "death by a thousand cuts" waste (unnecessary network round trips, disk I/O, repeated allocations, O(n^2) scans where O(1) hash lookups are equally simple).
2. **Measure Before Modifying:** Never optimize on intuition. Profile to locate exact bottlenecks and establish a baseline benchmark before changing code. If a change adds complexity without a measurable speedup, revert it.
3. **Prefer Fundamental Fixes:** Try algorithmic improvements, caching, batching, or eliminating I/O before micro-optimizing code structure.
4. **Design Around the Critical Path:** As a last resort on proven hot paths, sketch the minimal "ideal" code for the common case, collapse shallow wrapper layers, and consolidate special-case checks into a single fast-path guard that branches off-path for rare conditions.

## Language Guidance

For detailed Good vs. Bad idioms in **Python** (primary), **Rust**, and **Go**, read `references/language-idioms.md`. Key defaults:

- **Python:** Prefer deep domain modules with `dataclass(frozen=True)` or Pydantic models over raw dicts/tuples and multi-layer pass-through Service/Repository boilerplate. Use `Protocol` + composition over deep inheritance. Aggregate domain errors at API/worker boundaries.
- **Rust:** Keep `pub` surfaces narrow (`pub(crate)`), encode invariants in newtypes and enums to define errors out of existence, and distinguish recoverable `Result` boundaries from unrecoverable bugs.
- **Go:** Allow short variable names only when declaration and all uses fit within a few lines; use precise names for wider scopes and never overload one short name for two meanings. Design zero-values to work without initialization.

## Pre-Completion Verification

Before finalizing any code, refactor, or design proposal, verify every item:

- [ ] Can a caller use each public class or function correctly from its signature and docstring alone, without reading the body?
- [ ] Are there zero pass-through methods, shallow single-use wrappers, or temporal splits leaking the same format/protocol?
- [ ] Were unnecessary exceptions and `None`/empty special cases designed out of existence or aggregated at one boundary?
- [ ] Are all public return values and multi-field records named types rather than opaque tuples or generic dicts?
- [ ] Does every comment add information not obvious from adjacent code (precision or high-level intent), with zero implementation leaks in interface docstrings?
- [ ] Do all names create an unambiguous mental image and match existing repository conventions?

## Gotchas (Taking Principles Too Far)

Every design principle fails if pushed to an extreme. Watch for these boundaries:

- **Over-hiding information:** Never hide parameters, errors, or semantics that callers genuinely need (e.g., swallowing network failures silently so callers cannot tell if data was lost, or hiding cache-flush guarantees from a database caller).
- **Over-generalizing:** If callers must write loops or heavy glue code to do the common task, the API is too generic. Keep the interface easy for today's needs.
- **God modules:** Pulling unrelated functionality into one class creates a special-general mixture. Pull complexity down only when it belongs to the module's core abstraction and simplifies the interface.
- **Forcing design patterns:** Apply patterns (Decorator, Factory, Strategy) only when they genuinely reduce complexity; decorators in particular often create shallow pass-through wrappers.
- **Over-zealous consistency:** Similar things must look similar, and genuinely different things must look different. Never force unrelated concepts into the same name or pattern.
