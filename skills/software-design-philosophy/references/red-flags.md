# Software Design Red Flags Catalog

Use this checklist during design, self-review, and PR code reviews. Each red flag signals unnecessary complexity (dependencies or obscurity). When you spot one, pause and explore an alternative design that eliminates it.

## 1. Shallow Module

- **Symptom:** A class, module, or function has an interface nearly as complex as its implementation, or wraps 1-3 lines of logic with boilerplate documentation longer than the body. Common in "classitis" architectures (excessive tiny helper classes or arbitrary "split every function over 15 lines" rules).
- **Why it hurts:** Adds cognitive load (another interface to learn) without hiding meaningful complexity.
- **Fix:** Merge related shallow modules into a deeper module, inline single-use trivial helpers into their call site, or raise the abstraction level so one call accomplishes a complete logical task.

## 2. Information Leakage

- **Symptom:** The same design decision (file format, wire protocol, schema layout, encoding rule, internal data structure) is known by two or more modules. Worse when it is "back-door leakage" not even visible in the public API.
- **Why it hurts:** Causes change amplification and unknown unknowns; changing one format breaks distant modules silently.
- **Fix:** Merge the modules sharing the knowledge, or extract a single deep module that owns that knowledge completely behind an interface that does not expose internal representations (e.g., avoid returning raw mutable internal dicts/maps).

## 3. Temporal Decomposition

- **Symptom:** Modules or classes are split strictly by runtime execution stages (e.g., `FileReader`, `FileProcessor`, `FileWriter`, or `RequestSocketReader` and `RequestParser`).
- **Why it hurts:** Causes information leakage because early and late stages almost always depend on the same format or protocol rules.
- **Fix:** Organize modules around shared knowledge and capabilities (e.g., one `HttpMessage` or `DocumentCodec` module handling both reading/parsing and serialization).

## 4. Overexposure

- **Symptom:** Callers using the common case are forced to configure, instantiate, or understand rarely used options, flags, or wrapper layers (like manually wrapping file streams just to enable buffering).
- **Why it hurts:** Inflates cognitive load for the 95% use case.
- **Fix:** Make the common case automatic with smart defaults ("do the right thing without being asked"). Expose overrides through separate optional arguments or secondary methods that typical callers never need to see.

## 5. Pass-Through Method

- **Symptom:** A method does almost nothing except forward its arguments to another method with the same or nearly identical signature. Common in excessive layering (Controller -> Service -> Manager -> Repository) and overused Decorators.
- **Why it hurts:** Makes classes shallower, adds boilerplate, and couples signatures across layers without adding abstraction.
- **Fix:** Expose the lower-level class directly to callers, redistribute responsibilities cleanly between the two layers, or merge the classes. Note: dispatchers/routers and polymorphic interface implementations with distinct behavior are NOT pass-through methods.

## 6. Pass-Through Variable

- **Symptom:** A parameter (such as `config`, `cert`, `trace_id`, `tenant_id`, or `timeout`) is threaded through a long chain of intermediate functions that never use it, solely to reach a low-level call.
- **Why it hurts:** Forces every intermediate signature to know about unrelated data and causes massive change amplification when adding a new cross-cutting field.
- **Fix:** Store the value on an object already shared by top and bottom layers, or group system/request-scoped state into an immutable `Context` object held on long-lived service instances (injected via constructors) rather than passed through every method. Keep the context disciplined so it does not become an opaque grab-bag.

## 7. Repetition

- **Symptom:** The same or nearly identical code pattern (error formatting, cleanup sequence, retry loop, validation block) appears repeatedly across branches or modules.
- **Why it hurts:** Signals a missing abstraction and triggers change amplification when behavior changes.
- **Fix:** Extract a clean helper with a simple signature, use a context manager / RAII guard / `defer` for shared cleanup, or centralize error handling via exception aggregation. Avoid extracting 1-2 line snippets if doing so requires a complicated parameter list.

## 8. Special-General Mixture

- **Symptom:** A general-purpose mechanism (e.g., a text buffer, cache, job queue, or undo history) contains logic tailored to one specific caller or UI action (e.g., `text_buffer.handle_backspace()`).
- **Why it hurts:** Leaks caller specifics into reusable infrastructure, bloats the lower layer's interface, and prevents independent evolution.
- **Fix:** Keep the lower layer general-purpose (`insert(pos, text)`, `delete(start, end)`) and pull special-purpose policy upwards into the caller layer.

## 9. Conjoined Methods

- **Symptom:** Two methods or code blocks are physically separated, yet you cannot understand or safely modify one without reading the implementation of the other.
- **Why it hurts:** Breaks modular reasoning and forces constant jumping back and forth.
- **Fix:** Merge them back into a single coherent method, or redesign the child method's boundary so it performs a cleanly separable, self-contained subtask.

## 10. Comment Repeats Code

- **Symptom:** Comments paraphrase the exact statement or reuse the same words as the function/variable name (e.g., `# Get user by id` above `def get_user_by_id`, or `# Horizontal padding` above `HORIZONTAL_PADDING = 4`).
- **Why it hurts:** Adds visual noise and maintenance burden with zero new information.
- **Fix:** Delete comments that state the obvious. Replace echoed-name comments with different words that add missing precision (units, bounds, side effects, invariants) or high-level intent ("why").

## 11. Implementation Documentation Contaminates Interface

- **Symptom:** A class or public method docstring explains internal algorithms, private fields, internal helper calls, or underlying RPC/table names that callers do not need to know.
- **Why it hurts:** Exposes implementation details, increases caller cognitive load, and makes interface docs rot whenever internals change.
- **Fix:** Move internal mechanics into implementation comments inside the method body. Restrict interface docstrings strictly to caller-visible behavior, arguments, returns, preconditions, side effects, and exceptions.

## 12. Vague Name or Hard to Pick Name

- **Symptom:** Generic identifiers (`data`, `info`, `manager`, `status`, `x`, `y`, `result` in a void function, or one name like `block` used for both logical and physical blocks), OR you struggle to invent a short, precise name for a variable or method.
- **Why it hurts:** Causes silent misassumptions and subtle bugs. Difficulty naming usually means the entity conflates multiple responsibilities.
- **Fix:** Choose concrete, domain-specific nouns and boolean predicates (`cursor_visible`, `file_block_idx`, `disk_block_idx`). If no clean name fits, refactor or split the entity until its purpose is crisp.

## 13. Hard to Describe

- **Symptom:** Writing a complete, accurate interface comment for a method, class, or variable requires a long, convoluted paragraph full of caveats and special cases.
- **Why it hurts:** Strong indicator of a leaky, shallow, or tangled abstraction.
- **Fix:** Simplify the abstraction before implementing: eliminate special cases, reduce preconditions, split unrelated responsibilities, or pull complexity downwards.

## 14. Nonobvious Code

- **Symptom:** A reader cannot accurately guess what the code does on a quick pass. Common triggers: generic containers (`Pair`, unnamed tuples, untyped dicts), mismatched declared vs. runtime types, hidden side effects (constructors spawning threads/tasks), and indirect event/callback flows.
- **Why it hurts:** Creates obscurity and unknown unknowns, slowing changes and causing regressions.
- **Fix:** Use named domain containers, align types, follow standard conventions, add whitespace to group logical phases, and place explicit comments wherever behavior breaks standard expectations (e.g., documenting when an event handler is invoked).
